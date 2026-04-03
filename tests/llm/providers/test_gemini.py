from types import SimpleNamespace
import unittest

from google.genai import types

from myopenclaw.conversation.message import (
    MessageRole,
    SessionMessage,
    ToolCall,
    ToolCallBatch,
    ToolCallResult,
)
from myopenclaw.llm.providers.gemini import GeminiProvider
from myopenclaw.runtime import GenerateRequest
from myopenclaw.tools.base import ToolSpec


class GeminiProviderTests(unittest.TestCase):
    def test_build_tools_maps_tool_specs_to_gemini_function_declarations(self) -> None:
        declarations = GeminiProvider._build_tools(
            [
                ToolSpec(
                    name="echo",
                    description="Echo text",
                    input_schema={
                        "type": "object",
                        "properties": {
                            "text": {"type": "string"},
                        },
                        "required": ["text"],
                    },
                    output_schema={
                        "type": "object",
                        "properties": {
                            "output": {"type": "string"},
                        },
                        "required": ["output"],
                    },
                )
            ]
        )

        self.assertEqual(1, len(declarations))
        self.assertEqual("echo", declarations[0].function_declarations[0].name)
        self.assertEqual(
            {
                "type": "object",
                "properties": {
                    "output": {"type": "string"},
                },
                "required": ["output"],
            },
            declarations[0].function_declarations[0].response_json_schema,
        )

    def test_build_contents_maps_tool_batch_to_ordered_function_calls_and_results(self) -> None:
        request = GenerateRequest(
            system_instruction="You are Pickle.",
            messages=[
                SessionMessage(role=MessageRole.USER, content="hello"),
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
                                thought_signature=b"sig-1",
                            )
                        ],
                        results=[
                            ToolCallResult(
                                call_id="call-1",
                                content="pong",
                                metadata={
                                    "exit_code": 0,
                                    "cwd": "/tmp/workspace",
                                },
                            )
                        ],
                    ),
                ),
            ],
        )

        contents = GeminiProvider._build_contents(request.messages)

        self.assertEqual(["user", "model", "user"], [content.role for content in contents])
        self.assertEqual("hello", contents[0].parts[0].text)
        self.assertEqual("echo", contents[1].parts[-1].function_call.name)
        self.assertEqual(b"sig-1", contents[1].parts[-1].thought_signature)
        self.assertEqual("echo", contents[2].parts[0].function_response.name)
        self.assertEqual(
            {
                "output": "pong",
                "metadata": {
                    "exit_code": 0,
                    "cwd": "/tmp/workspace",
                },
            },
            contents[2].parts[0].function_response.response,
        )

    def test_build_contents_maps_error_tool_batch_results_to_error_payload(self) -> None:
        contents = GeminiProvider._build_contents(
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
                                arguments={"text": "hello"},
                            )
                        ],
                        results=[
                            ToolCallResult(
                                call_id="call-1",
                                content="command failed",
                                is_error=True,
                            )
                        ],
                    ),
                )
            ]
        )

        self.assertEqual(
            {"error": "command failed"},
            contents[1].parts[0].function_response.response,
        )

    def test_extract_tool_calls_reads_function_calls_from_response(self) -> None:
        response = SimpleNamespace(
            function_calls=[
                types.FunctionCall(
                    id="call-1",
                    name="echo",
                    args={"text": "hello"},
                )
            ],
            candidates=[
                SimpleNamespace(
                    content=SimpleNamespace(
                        parts=[
                            types.Part(
                                function_call=types.FunctionCall(
                                    id="call-1",
                                    name="echo",
                                    args={"text": "hello"},
                                ),
                                thought_signature=b"sig-1",
                            )
                        ]
                    )
                )
            ],
        )

        tool_calls = GeminiProvider._extract_tool_calls(response)

        self.assertEqual(
            [
                ToolCall(
                    id="call-1",
                    name="echo",
                    arguments={"text": "hello"},
                    thought_signature=b"sig-1",
                )
            ],
            tool_calls,
        )

    def test_extract_text_prefers_candidate_parts_over_response_text_property(self) -> None:
        class ResponseWithExplodingText:
            @property
            def text(self) -> str:
                raise AssertionError("response.text should not be accessed")

        response = ResponseWithExplodingText()
        response.candidates = [
            SimpleNamespace(
                content=SimpleNamespace(
                    parts=[
                        types.Part(text="first"),
                        types.Part(
                            function_call=types.FunctionCall(
                                id="call-1",
                                name="echo",
                                args={"text": "hello"},
                            )
                        ),
                        types.Part(text="second"),
                    ]
                )
            )
        ]

        text = GeminiProvider._extract_text(response)

        self.assertEqual("first\nsecond", text)

    def test_extract_text_does_not_fallback_when_parts_exist_but_have_no_text(self) -> None:
        class ResponseWithExplodingText:
            @property
            def text(self) -> str:
                raise AssertionError("response.text should not be accessed")

        response = ResponseWithExplodingText()
        response.candidates = [
            SimpleNamespace(
                content=SimpleNamespace(
                    parts=[
                        types.Part(
                            function_call=types.FunctionCall(
                                id="call-1",
                                name="echo",
                                args={"text": "hello"},
                            )
                        )
                    ]
                )
            )
        ]

        text = GeminiProvider._extract_text(response)

        self.assertEqual("", text)

    def test_extract_provider_finish_metadata_reads_primary_candidate(self) -> None:
        response = SimpleNamespace(
            response_id="resp-1",
            model_version="gemini-3-flash-preview-001",
            candidates=[
                SimpleNamespace(
                    finish_reason="MAX_TOKENS",
                    finish_message="Token budget exhausted.",
                )
            ],
        )

        self.assertEqual("MAX_TOKENS", GeminiProvider._extract_provider_finish_reason(response))
        self.assertEqual(
            "Token budget exhausted.",
            GeminiProvider._extract_provider_finish_message(response),
        )
        self.assertEqual("resp-1", response.response_id)
        self.assertEqual("gemini-3-flash-preview-001", response.model_version)

    def test_extract_usage_reads_extended_token_counters(self) -> None:
        response = SimpleNamespace(
            usage_metadata=SimpleNamespace(
                prompt_token_count=11,
                candidates_token_count=7,
                cached_content_token_count=3,
                thoughts_token_count=5,
                tool_use_prompt_token_count=2,
                total_token_count=28,
            )
        )

        usage = GeminiProvider._extract_usage(response)

        self.assertEqual(11, usage.input_tokens)
        self.assertEqual(7, usage.output_tokens)
        self.assertEqual(3, usage.cached_content_tokens)
        self.assertEqual(5, usage.thoughts_tokens)
        self.assertEqual(2, usage.tool_use_prompt_tokens)
        self.assertEqual(28, usage.total_tokens)


if __name__ == "__main__":
    unittest.main()
