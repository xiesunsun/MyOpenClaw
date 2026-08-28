"""OpenAI Responses Provider 的映射、流式与错误合同。"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from pickel.artifacts.artifact_service import ArtifactService
from pickel.artifacts.in_memory_blob_store import InMemoryBlobStore
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
    ArtifactBlock,
    TextBlock,
    ThinkingBlock,
    ToolCallBlock,
)
from pickel.persistence.in_memory_runtime_store import InMemoryRuntimeStore
from pickel.providers.openai import OpenAIResponsesProvider
from pickel.shared.frozen_json import thaw_json
from pickel.providers.stream import (
    StreamCompleted,
    TextDelta,
    ThinkingDelta,
    ToolCallArgsDelta,
    accumulate,
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
            ),
        ),
        tools=(
            ToolDefinition(
                "lookup",
                "lookup value",
                {"type": "object", "properties": {"query": {"type": "string"}}},
                {"type": "object"},
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
    provider = OpenAIResponsesProvider(
        model="gpt-5.6-luna",
        max_output_tokens=128,
        provider_options={
            "reasoning_effort": "medium",
            "reasoning_summary": "auto",
            "parallel_tool_calls": True,
        },
    )

    snapshot = thaw_json(provider.prepare(_context()).body)
    assert isinstance(snapshot, dict)
    assert snapshot.pop("stream") is True
    asyncio.run(provider.client.aclose())

    assert snapshot["store"] is False
    assert "previous_response_id" not in snapshot
    assert snapshot["instructions"] == "system"
    assert snapshot["reasoning"] == {"effort": "medium", "summary": "auto"}
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
            "output": "result",
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


def test_tool_result_image_is_mapped_as_function_output_content() -> None:
    artifact_service = ArtifactService(
        artifact_store=InMemoryRuntimeStore(),
        blob_store=InMemoryBlobStore(),
    )
    reference = artifact_service.create_artifact(
        data=b"\x89PNG\r\n\x1a\nimage",
        media_type="image/png",
        display_name="chart.png",
    )
    context = ModelContext(
        system=SystemContent(),
        messages=(
            AssistantMessage((ToolCallBlock("call-1", "read", {"path": "chart.png"}),)),
            ToolResultMessage(
                tool_call_id="call-1",
                tool_name="read",
                content=(ArtifactBlock(reference, alt_text="chart.png"),),
            ),
        ),
    )
    provider = OpenAIResponsesProvider(
        model="vision-model",
        artifact_service=artifact_service,
    )

    output = thaw_json(provider.prepare(context).body)["input"][1]["output"]
    asyncio.run(provider.client.aclose())

    assert output[0]["type"] == "input_image"
    assert output[0]["image_url"].startswith("data:image/png;base64,")


def test_count_context_tokens_uses_responses_input_tokens_request_shape() -> None:
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, json.loads(request.content)))
        return httpx.Response(
            200,
            json={"object": "response.input_tokens", "input_tokens": 123},
        )

    client = httpx.AsyncClient(
        base_url="https://example.test/v1/",
        transport=httpx.MockTransport(handler),
    )
    provider = OpenAIResponsesProvider(
        model="gpt-test",
        max_output_tokens=128,
        provider_options={"reasoning_effort": "medium"},
        client=client,
    )

    count = asyncio.run(provider.count_context_tokens(_context()))
    asyncio.run(client.aclose())

    assert count == 123
    assert seen[0][0] == "/v1/responses/input_tokens"
    payload = seen[0][1]
    assert payload["model"] == "gpt-test"
    assert payload["instructions"] == "system"
    assert payload["input"]
    assert payload["tools"]
    assert payload["reasoning"] == {"effort": "medium"}
    assert "stream" not in payload
    assert "store" not in payload
    assert "max_output_tokens" not in payload


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
    provider = OpenAIResponsesProvider(model="gpt-5.6-luna", client=client)

    async def collect():
        return [
            delta
            async for delta in provider.stream_prepared(provider.prepare(_context()))
        ]

    deltas = asyncio.run(collect())
    message = asyncio.run(
        accumulate(provider.stream_prepared(provider.prepare(_context())))
    )
    asyncio.run(client.aclose())

    assert captured == thaw_json(provider.prepare(_context()).body)
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
    provider = OpenAIResponsesProvider(model="test", client=client)

    with pytest.raises(ValueError, match="arguments"):
        asyncio.run(accumulate(provider.stream_prepared(provider.prepare(_context()))))
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
    provider = OpenAIResponsesProvider(model="test", client=client)

    with pytest.raises(RuntimeError, match="gateway failed"):
        asyncio.run(accumulate(provider.stream_prepared(provider.prepare(_context()))))
    asyncio.run(client.aclose())
