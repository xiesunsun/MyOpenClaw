import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

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
    ImageContent,
    TextContent,
    ThinkingContent,
    ToolCallContent,
)
from pickel.providers.anthropic import AnthropicProvider


class FakeAsyncMessageStream:
    def __init__(self, final_message) -> None:
        self.final_message = final_message

    def __aiter__(self):
        async def _no_events():
            return
            yield  # pragma: no cover

        return _no_events()

    async def get_final_message(self):
        return self.final_message


class FakeAsyncMessageStreamManager:
    def __init__(self, final_message) -> None:
        self.final_message = final_message

    async def __aenter__(self):
        return FakeAsyncMessageStream(self.final_message)

    async def __aexit__(self, exc_type, exc, exc_tb) -> None:
        return None


class AnthropicProviderTests(unittest.TestCase):
    def test_build_tools_maps_tool_definitions(self) -> None:
        tools = AnthropicProvider._build_tools(
            [
                ToolDefinition(
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

    def test_build_tools_keeps_mcp_json_schema_composition(self) -> None:
        schema = {
            "type": "object",
            "$defs": {"query": {"type": "string"}},
            "properties": {
                "query": {
                    "anyOf": [
                        {"$ref": "#/$defs/query"},
                        {"type": "null"},
                    ]
                }
            },
        }

        tool = AnthropicProvider._build_tools(
            [ToolDefinition(name="search", description="Search", input_schema=schema)]
        )[0]

        self.assertEqual(schema, tool["input_schema"])
        self.assertNotIn("strict", tool)

    def test_build_messages_thinking_tool_use_and_results(self) -> None:
        messages = AnthropicProvider._build_messages(
            [
                UserMessage(content=[TextContent(text="hello")]),
                AssistantMessage(
                    content=[
                        ThinkingContent(text="internal", signature="sig-1"),
                        TextContent(text="Let me check."),
                        ToolCallContent(
                            id="call-1",
                            name="echo",
                            arguments={"text": "ping"},
                        ),
                    ]
                ),
                ToolResultMessage(
                    tool_call_id="call-1",
                    tool_name="echo",
                    content=[TextContent(text="pong")],
                ),
                AssistantMessage(
                    content=[
                        ThinkingContent(text="final", signature="sig-2"),
                        TextContent(text="Done."),
                    ]
                ),
            ]
        )

        self.assertEqual(
            ["user", "assistant", "user", "assistant"],
            [m["role"] for m in messages],
        )
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
                AssistantMessage(
                    content=[
                        ToolCallContent(
                            id="call-1", name="echo", arguments={"text": "ping"}
                        )
                    ]
                ),
                ToolResultMessage(
                    tool_call_id="call-1",
                    tool_name="echo",
                    content=[TextContent(text="failed")],
                    is_error=True,
                ),
            ]
        )
        self.assertTrue(messages[1]["content"][0]["is_error"])

    def test_build_messages_maps_image_tool_result(self) -> None:
        messages = AnthropicProvider._build_messages(
            [
                AssistantMessage(
                    content=[ToolCallContent(id="call-1", name="look", arguments={})]
                ),
                ToolResultMessage(
                    tool_call_id="call-1",
                    tool_name="look",
                    content=[
                        TextContent(text="截图"),
                        ImageContent(media_type="image/png", data_base64="aGk="),
                    ],
                ),
            ]
        )

        content = messages[1]["content"][0]["content"]
        self.assertEqual("text", content[0]["type"])
        self.assertEqual("image", content[1]["type"])
        self.assertEqual("base64", content[1]["source"]["type"])
        self.assertEqual("image/png", content[1]["source"]["media_type"])

    def test_build_messages_aggregates_consecutive_tool_results(self) -> None:
        messages = AnthropicProvider._build_messages(
            [
                UserMessage(content=[TextContent(text="hi")]),
                AssistantMessage(
                    content=[
                        ToolCallContent(id="c1", name="a", arguments={}),
                        ToolCallContent(id="c2", name="b", arguments={}),
                    ]
                ),
                ToolResultMessage(
                    tool_call_id="c1",
                    tool_name="a",
                    content=[TextContent(text="r1")],
                ),
                ToolResultMessage(
                    tool_call_id="c2",
                    tool_name="b",
                    content=[TextContent(text="r2")],
                ),
            ]
        )
        self.assertEqual(["user", "assistant", "user"], [m["role"] for m in messages])
        self.assertEqual(2, len(messages[2]["content"]))
        self.assertEqual("c1", messages[2]["content"][0]["tool_use_id"])
        self.assertEqual("c2", messages[2]["content"][1]["tool_use_id"])

    def test_generate_maps_response_blocks_and_metadata(self) -> None:
        provider = AnthropicProvider(
            model="claude-opus-4-7",
            temperature=0.2,
            max_output_tokens=2048,
            provider_options={"thinking": "xhigh"},
        )
        stream = Mock(
            return_value=FakeAsyncMessageStreamManager(
                SimpleNamespace(
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
        )
        provider.client = SimpleNamespace(
            messages=SimpleNamespace(
                stream=stream,
                count_tokens=AsyncMock(),
            )
        )

        result = asyncio.run(
            provider.generate(
                ModelContext(
                    system=SystemContent.from_text("You are Pickle."),
                    messages=[UserMessage(content=[TextContent(text="hello")])],
                    tools=[
                        ToolDefinition(
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

        self.assertIsInstance(result, AssistantMessage)
        self.assertEqual("thinking", result.content[0].type)
        self.assertEqual("internal", result.content[0].text)
        self.assertEqual("sig-1", result.content[0].signature)
        self.assertEqual("I'll use a tool.", result.content[1].text)
        self.assertEqual("tool-1", result.content[2].id)
        self.assertEqual("echo", result.content[2].name)
        self.assertEqual({"text": "hello"}, result.content[2].arguments)
        self.assertEqual("tool_calls", result.metadata.finish_reason)
        self.assertEqual("msg-1", result.metadata.provider_response_id)
        self.assertEqual(11, result.metadata.usage.input_tokens)
        self.assertEqual(7, result.metadata.usage.output_tokens)
        self.assertEqual(3, result.metadata.usage.cache_read_tokens)
        self.assertEqual(2, result.metadata.usage.cache_write_tokens)

        create_kwargs = stream.call_args.kwargs
        self.assertEqual("You are Pickle.", create_kwargs["system"])
        self.assertEqual("claude-opus-4-7", create_kwargs["model"])
        self.assertNotIn("temperature", create_kwargs)

    def test_generate_sends_temperature_for_non_opus_models(self) -> None:
        provider = AnthropicProvider(
            model="claude-sonnet-4-0",
            temperature=0.4,
        )
        stream = Mock(
            return_value=FakeAsyncMessageStreamManager(
                SimpleNamespace(
                    id="msg-2",
                    model="claude-sonnet",
                    stop_reason="end_turn",
                    usage=None,
                    content=[SimpleNamespace(type="text", text="ok")],
                )
            )
        )
        provider.client = SimpleNamespace(
            messages=SimpleNamespace(stream=stream, count_tokens=AsyncMock())
        )
        asyncio.run(
            provider.generate(
                ModelContext(
                    system=SystemContent.from_text(""),
                    messages=[UserMessage(content=[TextContent(text="hello")])],
                )
            )
        )
        self.assertEqual(0.4, stream.call_args.kwargs["temperature"])

    def test_count_context_tokens_uses_matching_request_shape(self) -> None:
        provider = AnthropicProvider(model="claude-test")
        count_tokens = AsyncMock(return_value=SimpleNamespace(input_tokens=42))
        provider.client = SimpleNamespace(
            messages=SimpleNamespace(count_tokens=count_tokens, stream=Mock())
        )
        total = asyncio.run(
            provider.count_context_tokens(
                ModelContext(
                    system=SystemContent.from_text("sys"),
                    messages=[UserMessage(content=[TextContent(text="hello")])],
                    tools=[
                        ToolDefinition(
                            name="echo",
                            description="Echo",
                            input_schema={"type": "object"},
                        )
                    ],
                )
            )
        )
        self.assertEqual(42, total)
        kwargs = count_tokens.await_args.kwargs
        self.assertEqual("sys", kwargs["system"])
        self.assertEqual(1, len(kwargs["tools"]))

    def test_count_context_tokens_returns_none_on_failure(self) -> None:
        provider = AnthropicProvider(model="claude-test")
        provider.client = SimpleNamespace(
            messages=SimpleNamespace(
                count_tokens=AsyncMock(side_effect=RuntimeError("boom")),
                stream=Mock(),
            )
        )
        total = asyncio.run(
            provider.count_context_tokens(
                ModelContext(
                    system=SystemContent.from_text(""),
                    messages=[UserMessage(content=[TextContent(text="hello")])],
                )
            )
        )
        self.assertIsNone(total)

    def test_cache_control_marks_system_and_enables_automatic_caching(self) -> None:
        provider = AnthropicProvider(
            model="claude-test",
            provider_options={
                "cache_control": {
                    "type": "ephemeral",
                    "ttl": "5m",
                }
            },
        )

        params = provider._build_request_params(
            ModelContext(
                system=SystemContent.from_text("stable system"),
                messages=[UserMessage(content=[TextContent(text="hello")])],
                tools=[
                    ToolDefinition(
                        name="echo",
                        description="Echo",
                        input_schema={"type": "object"},
                    )
                ],
            )
        )

        expected = {"type": "ephemeral", "ttl": "5m"}
        self.assertEqual(expected, params["cache_control"])
        self.assertEqual(
            [
                {
                    "type": "text",
                    "text": "stable system",
                    "cache_control": expected,
                }
            ],
            params["system"],
        )
        self.assertNotIn("cache_control", params["tools"][-1])

    def test_request_snapshot_preserves_wire_request_and_cache_order(self) -> None:
        provider = AnthropicProvider(
            model="claude-test",
            max_output_tokens=2048,
            provider_options={"cache_control": {"type": "ephemeral"}},
        )
        context = ModelContext(
            system=SystemContent.from_text("stable system"),
            messages=[UserMessage(content=[TextContent(text="full user message")])],
            tools=[
                ToolDefinition(
                    name="echo",
                    description="Echo",
                    input_schema={"type": "object"},
                )
            ],
        )

        snapshot = provider.request_snapshot(context)

        self.assertEqual(["tools", "system", "messages"], snapshot["cache_order"])
        self.assertEqual(provider._build_create_params(context), snapshot["request"])
        self.assertEqual(
            "full user message",
            snapshot["request"]["messages"][0]["content"][0]["text"],
        )
        self.assertEqual("stable system", snapshot["request"]["system"][0]["text"])
        self.assertEqual("echo", snapshot["request"]["tools"][0]["name"])

    def test_cache_control_is_absent_by_default(self) -> None:
        provider = AnthropicProvider(model="claude-test")

        params = provider._build_request_params(
            ModelContext(
                system=SystemContent.from_text("system"),
                messages=[UserMessage(content=[TextContent(text="hello")])],
            )
        )

        self.assertNotIn("cache_control", params)
        self.assertEqual("system", params["system"])

    def test_cache_control_rejects_unsupported_ttl(self) -> None:
        with self.assertRaisesRegex(ValueError, "5m.*1h"):
            AnthropicProvider(
                model="claude-test",
                provider_options={
                    "cache_control": {
                        "type": "ephemeral",
                        "ttl": "24h",
                    }
                },
            )


if __name__ == "__main__":
    unittest.main()
