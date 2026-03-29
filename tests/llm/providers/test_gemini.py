from types import SimpleNamespace
import unittest

from google.genai import types

from myopenclaw.conversation.message import MessageRole, SessionMessage, ToolCall
from myopenclaw.llm.providers.gemini import GeminiProvider
from myopenclaw.runtime_protocols.generation import GenerateRequest
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
                )
            ]
        )

        self.assertEqual(1, len(declarations))
        self.assertEqual("echo", declarations[0].function_declarations[0].name)

    def test_build_contents_maps_tool_calls_and_tool_results(self) -> None:
        request = GenerateRequest(
            system_instruction="You are Pickle.",
            messages=[
                SessionMessage(role=MessageRole.USER, content="hello"),
                SessionMessage(
                    role=MessageRole.ASSISTANT,
                    tool_calls=[
                        ToolCall(
                            id="call-1",
                            name="echo",
                            arguments={"text": "ping"},
                            thought_signature=b"sig-1",
                        )
                    ],
                ),
                SessionMessage(
                    role=MessageRole.TOOL,
                    content="pong",
                    tool_call_id="call-1",
                    tool_name="echo",
                ),
            ],
        )

        contents = GeminiProvider._build_contents(request.messages)

        self.assertEqual(["user", "model", "user"], [content.role for content in contents])
        self.assertEqual("hello", contents[0].parts[0].text)
        self.assertEqual("echo", contents[1].parts[0].function_call.name)
        self.assertEqual(b"sig-1", contents[1].parts[0].thought_signature)
        self.assertEqual("echo", contents[2].parts[0].function_response.name)

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


if __name__ == "__main__":
    unittest.main()
