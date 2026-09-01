"""Stage 10：持久化 PreparedModelCall 与真实 Provider wire 必须完全一致。"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import Mock

import httpx

from pickel.context.model_context import ModelContext, SystemContent
from pickel.conversations.agent_message import UserMessage
from pickel.conversations.content_blocks import TextBlock
from pickel.providers.anthropic import AnthropicMessagesProvider
from pickel.providers.openai import OpenAIResponsesProvider
from pickel.providers.openai_chat_completions import OpenAIChatCompletionsProvider
from pickel.shared.frozen_json import thaw_json


def _context() -> ModelContext:
    return ModelContext(
        system=SystemContent.from_text("system"),
        messages=(UserMessage((TextBlock("hello"),)),),
        tools=(),
    )


class _AnthropicStream:
    def __init__(self, final) -> None:
        self.final = final

    def __aiter__(self):
        async def empty():
            if False:
                yield None

        return empty()

    async def get_final_message(self):
        return self.final


class _AnthropicManager:
    def __init__(self, final) -> None:
        self.final = final

    async def __aenter__(self):
        return _AnthropicStream(self.final)

    async def __aexit__(self, exc_type, exc, tb):
        return None


def test_anthropic_saved_body_equals_effective_sdk_wire() -> None:
    provider = AnthropicMessagesProvider(model="claude-test", api_key="test-key")
    stream = Mock(
        return_value=_AnthropicManager(
            SimpleNamespace(
                id="msg-1",
                model="claude-test",
                stop_reason="end_turn",
                usage=None,
                content=[SimpleNamespace(type="text", text="ok")],
            )
        )
    )
    provider.client = SimpleNamespace(messages=SimpleNamespace(stream=stream))
    prepared = provider.prepare(_context())

    async def collect():
        return [item async for item in provider.stream_prepared(prepared)]

    asyncio.run(collect())
    actual = {**stream.call_args.kwargs, "stream": True}
    assert actual == thaw_json(prepared.body)


def _responses_sse(final: dict) -> bytes:
    event = {"type": "response.completed", "response": final}
    return f"data: {json.dumps(event)}\n\ndata: [DONE]\n\n".encode()


def test_openai_responses_saved_body_is_exact_http_json() -> None:
    captured = {}
    final = {
        "id": "resp-1",
        "model": "gpt-test",
        "status": "completed",
        "output": [{"type": "message", "content": []}],
    }

    def respond(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_responses_sse(final),
        )

    client = httpx.AsyncClient(
        base_url="https://example.test/v1/",
        transport=httpx.MockTransport(respond),
    )
    provider = OpenAIResponsesProvider(model="gpt-test", client=client)
    prepared = provider.prepare(_context())

    async def collect():
        return [item async for item in provider.stream_prepared(prepared)]

    asyncio.run(collect())
    asyncio.run(client.aclose())
    assert captured == thaw_json(prepared.body)


def test_chat_completions_saved_body_is_exact_http_json() -> None:
    captured = {}

    def respond(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        event = {
            "id": "chat-1",
            "model": "chat-test",
            "choices": [
                {
                    "delta": {"content": "ok"},
                    "finish_reason": "stop",
                }
            ],
        }
        content = f"data: {json.dumps(event)}\n\ndata: [DONE]\n\n".encode()
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=content,
        )

    client = httpx.AsyncClient(
        base_url="https://example.test/v1/",
        transport=httpx.MockTransport(respond),
    )
    provider = OpenAIChatCompletionsProvider(model="chat-test", client=client)
    prepared = provider.prepare(_context())

    async def collect():
        return [item async for item in provider.stream_prepared(prepared)]

    asyncio.run(collect())
    asyncio.run(client.aclose())
    assert captured == thaw_json(prepared.body)
