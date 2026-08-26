"""OpenAI Responses Provider 的映射、流式与错误合同。"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from pickel.context.model_context import (
    ModelContext,
    SystemContent,
    ToolDefinition,
)
from pickel.conversations.agent_message import (
    AssistantMessage,
    ToolResultMessage,
    UserMessage,
)
from pickel.conversations.content_blocks import (
    TextBlock,
    ThinkingBlock,
    ToolCallBlock,
)
from pickel.providers.openai import OpenAIProvider
from pickel.providers.stream import (
    StreamCompleted,
    TextDelta,
    ThinkingDelta,
    ToolCallArgsDelta,
)


def _context() -> ModelContext:
    return ModelContext(
        system=SystemContent.from_text("system"),
        messages=(
            UserMessage((TextBlock("first"),)),
            AssistantMessage(
                (
                    ThinkingBlock("不应伪装成历史正文"),
                    TextBlock("checking"),
                    ToolCallBlock("call-1", "lookup", {"query": "first"}),
                )
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
                {"type": "object", "properties": {"query": {"type": "string"}}},
            ),
        ),
    )


def _sse(*events: dict) -> bytes:
    return (
        "".join(
            f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events
        )
        + "data: [DONE]\n\n"
    ).encode()


def _completed_response() -> dict:
    return {
        "id": "resp-1",
        "model": "gpt-5.6-luna-2026-08-01",
        "status": "completed",
        "output": [
            {
                "type": "reasoning",
                "summary": [{"type": "summary_text", "text": "brief thought"}],
            },
            {
                "type": "message",
                "content": [{"type": "output_text", "text": "calling"}],
            },
            {
                "type": "function_call",
                "id": "fc-2",
                "call_id": "call-2",
                "name": "lookup",
                "arguments": '{"query":"next"}',
            },
        ],
        "usage": {
            "input_tokens": 20,
            "output_tokens": 7,
            "total_tokens": 27,
            "input_tokens_details": {"cached_tokens": 5},
            "output_tokens_details": {"reasoning_tokens": 3},
        },
    }


def test_snapshot_is_stateless_responses_request_with_full_context() -> None:
    provider = OpenAIProvider(
        model="gpt-5.6-luna",
        max_output_tokens=128,
        provider_options={
            "reasoning_effort": "medium",
            "parallel_tool_calls": True,
        },
    )

    snapshot = provider.request_snapshot(_context())
    asyncio.run(provider.client.aclose())

    assert snapshot["store"] is False
    assert "previous_response_id" not in snapshot
    assert snapshot["instructions"] == "system"
    assert snapshot["reasoning"] == {"effort": "medium"}
    assert snapshot["parallel_tool_calls"] is True
    assert snapshot["input"] == [
        {
            "role": "user",
            "content": [{"type": "input_text", "text": "first"}],
        },
        {
            "role": "assistant",
            "content": [{"type": "output_text", "text": "checking"}],
        },
        {
            "type": "function_call",
            "call_id": "call-1",
            "name": "lookup",
            "arguments": '{"query":"first"}',
        },
        {
            "type": "function_call_output",
            "call_id": "call-1",
            "output": 'result\nstructured_content: {"ok":true}',
        },
    ]
    assert snapshot["tools"][0] == {
        "type": "function",
        "name": "lookup",
        "description": "lookup value",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
        },
    }


def test_stream_and_generate_share_response_parser() -> None:
    captured: dict = {}
    final = _completed_response()

    def respond(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_sse(
                {
                    "type": "response.output_item.added",
                    "item": {
                        "type": "function_call",
                        "id": "fc-2",
                        "call_id": "call-2",
                    },
                },
                {"type": "response.reasoning_summary_text.delta", "delta": "brief"},
                {"type": "response.output_text.delta", "delta": "calling"},
                {
                    "type": "response.function_call_arguments.delta",
                    "item_id": "fc-2",
                    "delta": '{"query":',
                },
                {"type": "response.completed", "response": final},
            ),
        )

    client = httpx.AsyncClient(
        base_url="https://cpa.example/v1/",
        transport=httpx.MockTransport(respond),
    )
    provider = OpenAIProvider(model="gpt-5.6-luna", client=client)

    async def collect():
        return [delta async for delta in provider.stream(_context())]

    deltas = asyncio.run(collect())
    message = asyncio.run(provider.generate(_context()))
    asyncio.run(client.aclose())

    assert captured == {**provider.request_snapshot(_context()), "stream": True}
    assert isinstance(deltas[0], ThinkingDelta)
    assert isinstance(deltas[1], TextDelta)
    assert deltas[2] == ToolCallArgsDelta("call-2", '{"query":')
    assert isinstance(deltas[-1], StreamCompleted)
    assert message.content == (
        ThinkingBlock("brief thought"),
        TextBlock("calling"),
        ToolCallBlock("call-2", "lookup", {"query": "next"}),
    )
    assert message.metadata is not None
    assert message.metadata.provider == "openai"
    assert message.metadata.provider_response_id == "resp-1"
    assert message.metadata.finish_reason == "tool_calls"
    assert message.metadata.usage is not None
    assert message.metadata.usage.cache_read_tokens == 5
    assert message.metadata.usage.reasoning_tokens == 3


def test_generate_rejects_invalid_function_arguments() -> None:
    final = _completed_response()
    final["output"] = [
        {
            "type": "function_call",
            "call_id": "call-1",
            "name": "lookup",
            "arguments": "not-json",
        }
    ]
    client = httpx.AsyncClient(
        base_url="https://cpa.example/v1/",
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=_sse({"type": "response.completed", "response": final}),
            )
        ),
    )
    provider = OpenAIProvider(model="test", client=client)

    with pytest.raises(ValueError, match="arguments"):
        asyncio.run(provider.generate(_context()))
    asyncio.run(client.aclose())


def test_failed_stream_is_not_mistaken_for_completed_response() -> None:
    client = httpx.AsyncClient(
        base_url="https://cpa.example/v1/",
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=_sse(
                    {
                        "type": "response.failed",
                        "response": {
                            "status": "failed",
                            "error": {"message": "gateway failed"},
                        },
                    }
                ),
            )
        ),
    )
    provider = OpenAIProvider(model="test", client=client)

    with pytest.raises(RuntimeError, match="gateway failed"):
        asyncio.run(provider.generate(_context()))
    asyncio.run(client.aclose())
