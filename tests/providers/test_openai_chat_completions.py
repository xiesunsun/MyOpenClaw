"""OpenAI-compatible Chat Completions 的 wire 合同。"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from pickel.context.model_context import ModelContext, SystemContent, ToolDefinition
from pickel.conversations.agent_message import (
    AssistantMessage,
    ToolResultMessage,
    UserMessage,
)
from pickel.conversations.content_blocks import TextBlock, ToolCallBlock
from pickel.providers.openai_chat_completions import OpenAIChatCompletionsProvider
from pickel.providers.stream import StreamCompleted, TextDelta, ToolCallArgsDelta


def _context() -> ModelContext:
    return ModelContext(
        system=SystemContent.from_text("system"),
        messages=(
            UserMessage((TextBlock("first"),)),
            AssistantMessage(
                (TextBlock("checking"), ToolCallBlock("call-1", "lookup", {"q": "x"}))
            ),
            ToolResultMessage(
                tool_call_id="call-1",
                tool_name="lookup",
                content=(TextBlock("result"),),
                structured_content={"ok": True},
            ),
        ),
        tools=(
            ToolDefinition(
                "lookup",
                "lookup value",
                {"type": "object", "properties": {"q": {"type": "string"}}},
            ),
        ),
    )


def _sse(*events: dict) -> bytes:
    return (
        "".join(f"data: {json.dumps(event)}\n\n" for event in events)
        + "data: [DONE]\n\n"
    ).encode()


def test_snapshot_maps_full_context_and_tools() -> None:
    provider = OpenAIChatCompletionsProvider(model="kimi-k3")

    snapshot = provider.request_snapshot(_context())
    asyncio.run(provider.client.aclose())

    assert snapshot["messages"] == [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "first"},
        {
            "role": "assistant",
            "content": "checking",
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "lookup", "arguments": '{"q":"x"}'},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call-1",
            "content": 'result\nstructured_content: {"ok":true}',
        },
    ]
    assert snapshot["tools"][0]["function"]["name"] == "lookup"


def test_stream_builds_provider_neutral_tool_call_and_usage() -> None:
    captured = {}

    def respond(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_sse(
                {
                    "id": "chat-1",
                    "model": "kimi-k3-202608",
                    "choices": [
                        {
                            "delta": {
                                "content": "calling",
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call-2",
                                        "function": {
                                            "name": "lookup",
                                            "arguments": '{"q":',
                                        },
                                    }
                                ],
                            },
                            "finish_reason": None,
                        }
                    ],
                },
                {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "function": {"arguments": '"next"}'},
                                    }
                                ]
                            },
                            "finish_reason": "tool_calls",
                        }
                    ]
                },
                {
                    "choices": [],
                    "usage": {
                        "prompt_tokens": 20,
                        "completion_tokens": 7,
                        "total_tokens": 27,
                    },
                },
            ),
        )

    client = httpx.AsyncClient(
        base_url="https://opencode.example/v1/",
        transport=httpx.MockTransport(respond),
    )
    provider = OpenAIChatCompletionsProvider(
        model="kimi-k3", provider_name="opencode-go", client=client
    )

    async def collect():
        return [delta async for delta in provider.stream(_context())]

    deltas = asyncio.run(collect())
    asyncio.run(client.aclose())

    assert captured == {
        **provider.request_snapshot(_context()),
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    assert isinstance(deltas[0], TextDelta)
    assert deltas[1] == ToolCallArgsDelta("call-2", '{"q":')
    assert deltas[2] == ToolCallArgsDelta("call-2", '"next"}')
    assert isinstance(deltas[-1], StreamCompleted)
    message = deltas[-1].message
    assert message.content == (
        TextBlock("calling"),
        ToolCallBlock("call-2", "lookup", {"q": "next"}),
    )
    assert message.metadata is not None
    assert message.metadata.provider == "opencode-go"
    assert message.metadata.provider_response_id == "chat-1"
    assert message.metadata.usage is not None
    assert message.metadata.usage.total_tokens == 27


def test_stream_rejects_truncated_response() -> None:
    client = httpx.AsyncClient(
        base_url="https://opencode.example/v1/",
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=b'data: {"choices":[{"delta":{"content":"cut"}}]}\n\n',
            )
        ),
    )
    provider = OpenAIChatCompletionsProvider(model="kimi-k3", client=client)

    with pytest.raises(ValueError, match="完成标志"):
        asyncio.run(
            provider.generate(ModelContext(system=SystemContent(), messages=()))
        )
    asyncio.run(client.aclose())
