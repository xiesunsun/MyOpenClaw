import asyncio
import base64
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from myopenclaw.context.model_context import (
    ModelContext,
    SystemContent,
    ToolDefinition,
)
from myopenclaw.conversations.agent_message import (
    AssistantMessage,
    ToolResultMessage,
    UserMessage,
)
from myopenclaw.conversations.content_blocks import TextContent, ToolCallContent
from myopenclaw.providers.gemini import GeminiProvider
from myopenclaw.shared.model_config import ModelConfig


class GeminiProviderTests(unittest.TestCase):
    def test_from_config_defaults_temperature_to_one_when_unset(self) -> None:
        provider = GeminiProvider.from_config(
            ModelConfig(
                provider="google/gemini",
                model="gemini-test",
                temperature=None,
            )
        )
        self.assertEqual(1.0, provider.temperature)

    def test_build_tools_maps_tool_definitions(self) -> None:
        tools = GeminiProvider._build_tools(
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
        self.assertEqual(1, len(tools))
        declaration = tools[0].function_declarations[0]
        self.assertEqual("echo", declaration.name)
        self.assertEqual("Echo text", declaration.description)

    def test_build_contents_maps_tool_calls_and_results(self) -> None:
        contents = GeminiProvider._build_contents(
            [
                UserMessage(content=[TextContent(text="hello")]),
                AssistantMessage(
                    content=[
                        TextContent(text="checking"),
                        ToolCallContent(
                            id="call-1",
                            name="echo",
                            arguments={"text": "ping"},
                            thought_signature=base64.b64encode(b"sig").decode("ascii"),
                        ),
                    ]
                ),
                ToolResultMessage(
                    tool_call_id="call-1",
                    tool_name="echo",
                    content=[TextContent(text="pong")],
                ),
            ]
        )
        self.assertEqual(3, len(contents))
        self.assertEqual("user", contents[0].role)
        self.assertEqual("model", contents[1].role)
        self.assertEqual("user", contents[2].role)
        self.assertIsNotNone(contents[1].parts[1].function_call)
        self.assertEqual("echo", contents[1].parts[1].function_call.name)
        self.assertIsNotNone(contents[2].parts[0].function_response)
        self.assertEqual(
            {"output": "pong"}, contents[2].parts[0].function_response.response
        )

    def test_build_contents_maps_error_tool_results(self) -> None:
        contents = GeminiProvider._build_contents(
            [
                AssistantMessage(
                    content=[
                        ToolCallContent(id="call-1", name="echo", arguments={})
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
        self.assertEqual(
            {"error": "failed"}, contents[1].parts[0].function_response.response
        )

    def test_build_contents_aggregates_multiple_tool_results(self) -> None:
        contents = GeminiProvider._build_contents(
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
        self.assertEqual(3, len(contents))
        self.assertEqual(2, len(contents[2].parts))
        self.assertEqual("c1", contents[2].parts[0].function_response.id)
        self.assertEqual("c2", contents[2].parts[1].function_response.id)

    def test_extract_tool_calls_reads_function_calls_from_response(self) -> None:
        response = SimpleNamespace(
            candidates=[
                SimpleNamespace(
                    content=SimpleNamespace(
                        parts=[
                            SimpleNamespace(
                                text=None,
                                function_call=SimpleNamespace(
                                    id="f1",
                                    name="echo",
                                    args={"text": "x"},
                                ),
                                thought_signature=b"sig-bytes",
                            )
                        ]
                    )
                )
            ],
            function_calls=None,
        )
        tools = GeminiProvider._extract_tool_call_contents(response)
        self.assertEqual(1, len(tools))
        self.assertEqual("f1", tools[0].id)
        self.assertEqual("echo", tools[0].name)
        self.assertEqual({"text": "x"}, tools[0].arguments)
        self.assertEqual(
            base64.b64encode(b"sig-bytes").decode("ascii"),
            tools[0].thought_signature,
        )

    def test_extract_text_prefers_candidate_parts(self) -> None:
        response = SimpleNamespace(
            candidates=[
                SimpleNamespace(
                    content=SimpleNamespace(
                        parts=[SimpleNamespace(text="from-parts", function_call=None)]
                    )
                )
            ],
            text="from-property",
        )
        self.assertEqual("from-parts", GeminiProvider._extract_text(response))

    def test_extract_text_does_not_fallback_when_parts_exist_but_have_no_text(
        self,
    ) -> None:
        response = SimpleNamespace(
            candidates=[
                SimpleNamespace(
                    content=SimpleNamespace(
                        parts=[SimpleNamespace(text=None, function_call=None)]
                    )
                )
            ],
            text="fallback",
        )
        self.assertEqual("", GeminiProvider._extract_text(response))

    def test_extract_usage_reads_extended_token_counters(self) -> None:
        response = SimpleNamespace(
            usage_metadata=SimpleNamespace(
                prompt_token_count=10,
                candidates_token_count=4,
                cached_content_token_count=2,
                thoughts_token_count=3,
                total_token_count=17,
            )
        )
        usage = GeminiProvider._extract_usage(response)
        self.assertEqual(10, usage.input_tokens)
        self.assertEqual(4, usage.output_tokens)
        self.assertEqual(2, usage.cache_read_tokens)
        self.assertEqual(3, usage.reasoning_tokens)
        self.assertEqual(17, usage.total_tokens)

    def test_count_context_tokens_uses_generate_content_request_shape(self) -> None:
        provider = GeminiProvider(model="gemini-test")
        provider.client = SimpleNamespace(
            _api_client=SimpleNamespace(
                async_request=AsyncMock(
                    return_value=SimpleNamespace(total_tokens=9, body=None)
                )
            )
        )
        total = asyncio.run(
            provider.count_context_tokens(
                ModelContext(
                    system=SystemContent.from_text("sys"),
                    messages=[UserMessage(content=[TextContent(text="hello")])],
                )
            )
        )
        self.assertEqual(9, total)
        request_dict = provider.client._api_client.async_request.await_args.kwargs[
            "request_dict"
        ]
        self.assertIn("generateContentRequest", request_dict)
        self.assertIn(
            "systemInstruction", request_dict["generateContentRequest"]
        )

    def test_count_context_tokens_serializes_tools_and_thought_signatures(self) -> None:
        provider = GeminiProvider(model="gemini-test")
        provider.client = SimpleNamespace(
            _api_client=SimpleNamespace(
                async_request=AsyncMock(
                    return_value=SimpleNamespace(total_tokens=3, body=None)
                )
            )
        )
        asyncio.run(
            provider.count_context_tokens(
                ModelContext(
                    system=SystemContent.from_text(""),
                    messages=[
                        AssistantMessage(
                            content=[
                                ToolCallContent(
                                    id="c1",
                                    name="echo",
                                    arguments={"text": "x"},
                                    thought_signature=base64.b64encode(b"sig").decode(
                                        "ascii"
                                    ),
                                )
                            ]
                        ),
                        ToolResultMessage(
                            tool_call_id="c1",
                            tool_name="echo",
                            content=[TextContent(text="ok")],
                        ),
                    ],
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
        request_dict = provider.client._api_client.async_request.await_args.kwargs[
            "request_dict"
        ]
        self.assertIn("tools", request_dict["generateContentRequest"])
        contents = request_dict["generateContentRequest"]["contents"]
        self.assertTrue(contents)

    def test_count_context_tokens_returns_zero_for_empty_request(self) -> None:
        provider = GeminiProvider(model="gemini-test")
        provider.client = SimpleNamespace(
            _api_client=SimpleNamespace(
                async_request=AsyncMock(
                    return_value=SimpleNamespace(total_tokens=0, body=None)
                )
            )
        )
        total = asyncio.run(
            provider.count_context_tokens(
                ModelContext(system=SystemContent.from_text(""), messages=[])
            )
        )
        self.assertEqual(0, total)

    def test_count_context_tokens_retries_after_transient_failure(self) -> None:
        provider = GeminiProvider(model="gemini-test")
        provider.client = SimpleNamespace(
            _api_client=SimpleNamespace(
                async_request=AsyncMock(
                    side_effect=[
                        RuntimeError("transient"),
                        SimpleNamespace(total_tokens=5, body=None),
                    ]
                )
            )
        )
        with patch("asyncio.sleep", new=AsyncMock()):
            total = asyncio.run(
                provider.count_context_tokens(
                    ModelContext(
                        system=SystemContent.from_text(""),
                        messages=[UserMessage(content=[TextContent(text="hello")])],
                    )
                )
            )
        self.assertEqual(5, total)

    def test_extract_count_tokens_total_reads_http_response_body(self) -> None:
        response = SimpleNamespace(total_tokens=None, body='{"totalTokens": 12}')
        self.assertEqual(12, GeminiProvider._extract_count_tokens_total(response))

    def test_build_generate_config_reads_provider_options_thinking(self) -> None:
        provider = GeminiProvider(
            model="gemini-test",
            provider_options={"thinking": "high"},
        )
        config = provider._build_generate_config(
            ModelContext(
                system=SystemContent.from_text("sys"),
                messages=[UserMessage(content=[TextContent(text="hello")])],
            )
        )
        self.assertIsNotNone(config.thinking_config)

    def test_generate_returns_assistant_message(self) -> None:
        provider = GeminiProvider(model="gemini-test")
        provider.client = SimpleNamespace(
            aio=SimpleNamespace(
                models=SimpleNamespace(
                    generate_content=AsyncMock(
                        return_value=SimpleNamespace(
                            candidates=[
                                SimpleNamespace(
                                    content=SimpleNamespace(
                                        parts=[
                                            SimpleNamespace(
                                                text="hello-back",
                                                function_call=None,
                                            )
                                        ]
                                    ),
                                    finish_message=None,
                                )
                            ],
                            response_id="r1",
                            model_version="v1",
                            usage_metadata=SimpleNamespace(
                                prompt_token_count=1,
                                candidates_token_count=2,
                                cached_content_token_count=None,
                                thoughts_token_count=None,
                                total_token_count=3,
                            ),
                            text="hello-back",
                            function_calls=None,
                        )
                    )
                )
            )
        )
        result = asyncio.run(
            provider.generate(
                ModelContext(
                    system=SystemContent.from_text("sys"),
                    messages=[UserMessage(content=[TextContent(text="hi")])],
                )
            )
        )
        self.assertIsInstance(result, AssistantMessage)
        self.assertEqual("hello-back", result.content[0].text)
        self.assertEqual("stop", result.metadata.finish_reason)
        self.assertEqual(1, result.metadata.usage.input_tokens)


if __name__ == "__main__":
    unittest.main()
