"""OpenAI-compatible Chat Completions 的 wire 合同。"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from pickel.artifacts.artifact_service import ArtifactService
from pickel.artifacts.in_memory_blob_store import InMemoryBlobStore
from pickel.context.model_context import ModelContext, SystemContent, ToolDefinition
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
from pickel.providers.openai_chat_completions import OpenAIChatCompletionsProvider
from pickel.shared.model_capability import GLM_5_3_FLASH_CAPABILITY_PROFILE
from pickel.shared.frozen_json import thaw_json
from pickel.providers.stream import (
    StreamCompleted,
    TextDelta,
    ToolCallArgsDelta,
    accumulate,
)


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
            ),
        ),
        tools=(
            ToolDefinition(
                "lookup",
                "lookup value",
                {"type": "object", "properties": {"q": {"type": "string"}}},
                {"type": "object"},
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

    snapshot = thaw_json(provider.prepare(_context()).body)
    assert isinstance(snapshot, dict)
    assert snapshot.pop("stream") is True
    snapshot.pop("stream_options", None)
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
            "content": "result",
        },
    ]
    assert snapshot["tools"][0]["function"]["name"] == "lookup"


def test_glm_snapshot_enables_tool_stream_and_preserves_thinking() -> None:
    context = ModelContext(
        system=SystemContent.from_text("system"),
        messages=(
            AssistantMessage(
                (
                    ThinkingBlock("先检查工具结果"),
                    TextBlock("继续执行"),
                    ToolCallBlock("call-1", "lookup", {"q": "x"}),
                )
            ),
        ),
        tools=_context().tools,
    )
    provider = OpenAIChatCompletionsProvider(
        model="glm-5.3-flash",
        provider_name="opencode-go",
        provider_options={
            "tool_stream": True,
            "preserve_thinking": True,
            "thinking": {"type": "enabled", "clear_thinking": False},
            "reasoning_effort": "max",
            "parallel_tool_calls": False,
        },
    )

    snapshot = thaw_json(provider.prepare(context).body)
    asyncio.run(provider.client.aclose())

    assert snapshot["tool_stream"] is True
    assert snapshot["thinking"] == {"type": "enabled", "clear_thinking": False}
    assert snapshot["reasoning_effort"] == "max"
    assert snapshot["parallel_tool_calls"] is False
    assert snapshot["messages"][1]["reasoning_content"] == "先检查工具结果"


def test_glm_capability_profile_supplies_wire_defaults() -> None:
    context = ModelContext(
        system=SystemContent.from_text("system"),
        messages=(
            AssistantMessage(
                (ThinkingBlock("previous"), TextBlock("done")),
            ),
        ),
    )
    provider = OpenAIChatCompletionsProvider(
        model="glm-5.3-flash",
        provider_name="opencode-go",
        capability_profile=GLM_5_3_FLASH_CAPABILITY_PROFILE,
    )

    snapshot = thaw_json(provider.prepare(context).body)
    asyncio.run(provider.client.aclose())

    assert snapshot["tool_stream"] is True
    assert snapshot["thinking"] == {"type": "enabled", "clear_thinking": False}
    assert snapshot["reasoning_effort"] == "max"
    assert snapshot["parallel_tool_calls"] is False
    assert snapshot["messages"][1]["reasoning_content"] == "previous"


def test_tool_result_images_follow_complete_tool_result_group() -> None:
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
            AssistantMessage(
                (
                    ToolCallBlock("call-1", "read", {"path": "chart.png"}),
                    ToolCallBlock("call-2", "grep", {"pattern": "x"}),
                )
            ),
            ToolResultMessage(
                tool_call_id="call-1",
                tool_name="read",
                content=(ArtifactBlock(reference, alt_text="chart.png"),),
            ),
            ToolResultMessage(
                tool_call_id="call-2",
                tool_name="grep",
                content=(TextBlock("match"),),
            ),
        ),
    )
    provider = OpenAIChatCompletionsProvider(
        model="vision-model",
        artifact_service=artifact_service,
    )

    messages = thaw_json(provider.prepare(context).body)["messages"]
    asyncio.run(provider.client.aclose())

    assert [message["role"] for message in messages] == [
        "assistant",
        "tool",
        "tool",
        "user",
    ]
    assert messages[1]["tool_call_id"] == "call-1"
    assert "image result attached" in messages[1]["content"]
    assert messages[2] == {
        "role": "tool",
        "tool_call_id": "call-2",
        "content": "match",
    }
    assert messages[3]["content"][1]["type"] == "image_url"
    assert messages[3]["content"][1]["image_url"]["url"].startswith(
        "data:image/png;base64,"
    )


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
        return [
            delta
            async for delta in provider.stream_prepared(provider.prepare(_context()))
        ]

    deltas = asyncio.run(collect())
    asyncio.run(client.aclose())

    assert captured == thaw_json(provider.prepare(_context()).body)
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


def test_stream_preserves_interleaved_reasoning_and_multiple_tool_indexes() -> None:
    client = httpx.AsyncClient(
        base_url="https://opencode.example/v1/",
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=_sse(
                    {
                        "id": "chat-multi",
                        "choices": [
                            {
                                "delta": {
                                    "reasoning_content": "先分析",
                                    "tool_calls": [
                                        {
                                            "index": 1,
                                            "id": "call-1",
                                            "function": {
                                                "name": "second",
                                                "arguments": '{"b":',
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
                                    "reasoning_content": "再验证",
                                    "content": "调用工具",
                                    "tool_calls": [
                                        {
                                            "index": 0,
                                            "id": "call-0",
                                            "function": {
                                                "name": "first",
                                                "arguments": '{"a":',
                                            },
                                        },
                                        {
                                            "index": 1,
                                            "function": {"arguments": "2}"},
                                        },
                                    ],
                                },
                                "finish_reason": None,
                            }
                        ]
                    },
                    {
                        "choices": [
                            {
                                "delta": {
                                    "tool_calls": [
                                        {
                                            "index": 0,
                                            "function": {"arguments": "1}"},
                                        }
                                    ]
                                },
                                "finish_reason": "tool_calls",
                            }
                        ]
                    },
                ),
            )
        ),
    )
    provider = OpenAIChatCompletionsProvider(model="glm-5.3-flash", client=client)

    async def collect():
        return [
            delta
            async for delta in provider.stream_prepared(provider.prepare(_context()))
        ]

    deltas = asyncio.run(collect())
    asyncio.run(client.aclose())

    assert [type(delta).__name__ for delta in deltas[:5]] == [
        "ThinkingDelta",
        "ToolCallArgsDelta",
        "ThinkingDelta",
        "TextDelta",
        "ToolCallArgsDelta",
    ]
    message = deltas[-1].message
    assert message.content == (
        ThinkingBlock("先分析再验证"),
        TextBlock("调用工具"),
        ToolCallBlock("call-0", "first", {"a": 1}),
        ToolCallBlock("call-1", "second", {"b": 2}),
    )


def test_stream_incomplete_error_keeps_partial_reasoning_for_recovery() -> None:
    client = httpx.AsyncClient(
        base_url="https://opencode.example/v1/",
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=(
                    'data: {"choices":[{"delta":{"reasoning_content":"partial"}}]}\n\n'
                ).encode(),
            )
        ),
    )
    provider = OpenAIChatCompletionsProvider(model="glm-5.3-flash", client=client)

    from pickel.providers.errors import ProviderStreamIncompleteError

    with pytest.raises(ProviderStreamIncompleteError) as caught:
        asyncio.run(
            accumulate(
                provider.stream_prepared(
                    provider.prepare(ModelContext(system=SystemContent(), messages=()))
                )
            )
        )
    asyncio.run(client.aclose())

    assert caught.value.assistant_message.content == (ThinkingBlock("partial"),)
    assert caught.value.http_status == 200


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
            accumulate(
                provider.stream_prepared(
                    provider.prepare(ModelContext(system=SystemContent(), messages=()))
                )
            )
        )
    asyncio.run(client.aclose())
