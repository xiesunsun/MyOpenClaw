import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from myopenclaw.conversations.message import (
    MessageRole,
    SessionMessage,
    ToolCall,
    ToolCallBatch,
    ToolCallResult,
)
from myopenclaw.providers.anthropic import AnthropicProvider
from myopenclaw.shared.generation import FinishReason, GenerateRequest
from myopenclaw.tools.base import ToolSpec


class AnthropicProviderTests(unittest.TestCase):
    def test_build_tools_maps_tool_specs_to_anthropic_tools(self) -> None:
        tools = AnthropicProvider._build_tools(
            [
                ToolSpec(
                    name="echo",
                    description="Echo text",
                    input_schema={
                        "type": "object",
                        "properties": {"text": {"type": "string"}},
                        "required": ["text"],
                    },
                )
            ]
        )

        self.assertEqual(
            [
                {
                    "name": "echo",
                    "description": "Echo text",
                    "input_schema": {
                        "type": "object",
                        "properties": {"text": {"type": "string"}},
                        "required": ["text"],
                    },
                }
            ],
            tools,
        )

    def test_build_messages_reconstructs_thinking_tool_use_and_tool_results(self) -> None:
        messages = AnthropicProvider._build_messages(
            [
                SessionMessage(role=MessageRole.USER, content="hello"),
                SessionMessage(
                    role=MessageRole.ASSISTANT,
                    content="Let me check.",
                    provider_thinking_blocks=[
                        {
                            "type": "thinking",
                            "thinking": "internal",
                            "signature": "sig-1",
                        }
                    ],
                    tool_call_batch=ToolCallBatch(
                        batch_id="batch-1",
                        step_index=1,
                        calls=[
                            ToolCall(
                                id="call-1",
                                name="echo",
                                arguments={"text": "ping"},
                            )
                        ],
                        results=[
                            ToolCallResult(
                                call_id="call-1",
                                content="pong",
                                metadata={"exit_code": 0},
                            )
                        ],
                    ),
                ),
                SessionMessage(
                    role=MessageRole.ASSISTANT,
                    content="Done.",
                    provider_thinking_blocks=[
                        {
                            "type": "thinking",
                            "thinking": "final",
                            "signature": "sig-2",
                        }
                    ],
                ),
            ]
        )

        self.assertEqual(["user", "assistant", "user", "assistant"], [m["role"] for m in messages])
        self.assertEqual([{"type": "text", "text": "hello"}], messages[0]["content"])
        self.assertEqual("thinking", messages[1]["content"][0]["type"])
        self.assertEqual("text", messages[1]["content"][1]["type"])
        self.assertEqual("tool_use", messages[1]["content"][2]["type"])
        self.assertEqual("echo", messages[1]["content"][2]["name"])
        self.assertEqual(
            [
                {
                    "type": "tool_result",
                    "tool_use_id": "call-1",
                    "content": "pong",
                }
            ],
            messages[2]["content"],
        )
        self.assertEqual("thinking", messages[3]["content"][0]["type"])
        self.assertEqual("Done.", messages[3]["content"][1]["text"])

    def test_build_messages_marks_error_tool_results(self) -> None:
        messages = AnthropicProvider._build_messages(
            [
                SessionMessage(
                    role=MessageRole.ASSISTANT,
                    tool_call_batch=ToolCallBatch(
                        batch_id="batch-1",
                        step_index=1,
                        calls=[
                            ToolCall(
                                id="call-1",
                                name="echo",
                                arguments={"text": "ping"},
                            )
                        ],
                        results=[
                            ToolCallResult(
                                call_id="call-1",
                                content="failed",
                                is_error=True,
                            )
                        ],
                    ),
                )
            ]
        )

        self.assertTrue(messages[1]["content"][0]["is_error"])

    def test_generate_maps_response_blocks_and_metadata(self) -> None:
        provider = AnthropicProvider(
            model="claude-opus-4-7",
            temperature=0.2,
            max_output_tokens=2048,
            provider_options={"thinking": "xhigh"},
        )
        create = AsyncMock(
            return_value=SimpleNamespace(
                id="msg-1",
                model="claude-opus-4-7-20250421",
                stop_reason="tool_use",
                usage=SimpleNamespace(
                    input_tokens=11,
                    output_tokens=7,
                    cache_creation_input_tokens=2,
                    cache_read_input_tokens=3,
                ),
                content=[
                    SimpleNamespace(
                        type="thinking",
                        thinking="internal",
                        signature="sig-1",
                    ),
                    SimpleNamespace(type="text", text="I'll use a tool."),
                    SimpleNamespace(
                        type="tool_use",
                        id="tool-1",
                        name="echo",
                        input={"text": "hello"},
                    ),
                ],
            )
        )
        provider.client = SimpleNamespace(
            messages=SimpleNamespace(
                create=create,
                count_tokens=AsyncMock(),
            )
        )

        result = asyncio.run(
            provider.generate(
                GenerateRequest(
                    system_instruction="You are Pickle.",
                    messages=[SessionMessage(role=MessageRole.USER, content="hello")],
                    tools=[
                        ToolSpec(
                            name="echo",
                            description="Echo text",
                            input_schema={
                                "type": "object",
                                "properties": {"text": {"type": "string"}},
                                "required": ["text"],
                            },
                        )
                    ],
                )
            )
        )

        self.assertEqual("I'll use a tool.", result.text)
        self.assertEqual(
            [ToolCall(id="tool-1", name="echo", arguments={"text": "hello"})],
            result.tool_calls,
        )
        self.assertEqual(FinishReason.TOOL_CALLS, result.finish_reason)
        self.assertEqual("tool_use", result.provider_finish_reason)
        self.assertEqual("msg-1", result.provider_response_id)
        self.assertEqual("claude-opus-4-7-20250421", result.provider_model_version)
        self.assertEqual(11, result.usage.input_tokens)
        self.assertEqual(7, result.usage.output_tokens)
        self.assertEqual(5, result.usage.cached_content_tokens)
        self.assertEqual(18, result.usage.total_tokens)
        self.assertEqual(
            [{"type": "thinking", "thinking": "internal", "signature": "sig-1"}],
            result.provider_thinking_blocks,
        )

        kwargs = create.await_args.kwargs
        self.assertEqual("claude-opus-4-7", kwargs["model"])
        self.assertEqual(2048, kwargs["max_tokens"])
        self.assertEqual("You are Pickle.", kwargs["system"])
        self.assertEqual(
            [{"role": "user", "content": [{"type": "text", "text": "hello"}]}],
            kwargs["messages"],
        )
        self.assertEqual(
            [{"name": "echo", "description": "Echo text", "input_schema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}}],
            kwargs["tools"],
        )
        self.assertEqual(
            {"type": "adaptive", "display": "summarized"},
            kwargs["thinking"],
        )
        self.assertEqual({"effort": "xhigh"}, kwargs["output_config"])
        self.assertNotIn("temperature", kwargs)

    def test_generate_sends_temperature_for_non_opus_models(self) -> None:
        provider = AnthropicProvider(
            model="claude-sonnet-4-0",
            temperature=0.3,
        )
        create = AsyncMock(
            return_value=SimpleNamespace(
                id="msg-1",
                model="claude-sonnet-4-0",
                stop_reason="end_turn",
                usage=None,
                content=[SimpleNamespace(type="text", text="done")],
            )
        )
        provider.client = SimpleNamespace(
            messages=SimpleNamespace(
                create=create,
                count_tokens=AsyncMock(),
            )
        )

        asyncio.run(
            provider.generate(
                GenerateRequest(
                    system_instruction=None,
                    messages=[SessionMessage(role=MessageRole.USER, content="hello")],
                )
            )
        )

        self.assertEqual(0.3, create.await_args.kwargs["temperature"])

    def test_count_request_tokens_uses_matching_request_shape(self) -> None:
        provider = AnthropicProvider(
            model="claude-opus-4-7",
            provider_options={"thinking": "high"},
        )
        count_tokens = AsyncMock(return_value=SimpleNamespace(input_tokens=42))
        provider.client = SimpleNamespace(
            messages=SimpleNamespace(
                create=AsyncMock(),
                count_tokens=count_tokens,
            )
        )

        total_tokens = asyncio.run(
            provider.count_request_tokens(
                GenerateRequest(
                    system_instruction="You are Pickle.",
                    messages=[SessionMessage(role=MessageRole.USER, content="hello")],
                    tools=[
                        ToolSpec(
                            name="echo",
                            description="Echo text",
                            input_schema={
                                "type": "object",
                                "properties": {"text": {"type": "string"}},
                                "required": ["text"],
                            },
                        )
                    ],
                )
            )
        )

        self.assertEqual(42, total_tokens)
        kwargs = count_tokens.await_args.kwargs
        self.assertEqual("claude-opus-4-7", kwargs["model"])
        self.assertEqual("You are Pickle.", kwargs["system"])
        self.assertEqual(
            [{"role": "user", "content": [{"type": "text", "text": "hello"}]}],
            kwargs["messages"],
        )
        self.assertEqual(
            {"type": "adaptive", "display": "summarized"},
            kwargs["thinking"],
        )
        self.assertEqual({"effort": "high"}, kwargs["output_config"])
        self.assertNotIn("max_tokens", kwargs)

    def test_count_request_tokens_returns_none_on_failure(self) -> None:
        provider = AnthropicProvider(model="claude-opus-4-7")
        provider.client = SimpleNamespace(
            messages=SimpleNamespace(
                create=AsyncMock(),
                count_tokens=AsyncMock(side_effect=RuntimeError("boom")),
            )
        )

        total_tokens = asyncio.run(
            provider.count_request_tokens(
                GenerateRequest(
                    system_instruction=None,
                    messages=[SessionMessage(role=MessageRole.USER, content="hello")],
                )
            )
        )

        self.assertIsNone(total_tokens)


if __name__ == "__main__":
    unittest.main()
