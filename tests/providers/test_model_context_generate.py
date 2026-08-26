"""Provider 消费同一 ModelContext 的结构与多轮 tool history 黄金用例。"""

from __future__ import annotations

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
from pickel.conversations.content_blocks import TextBlock, ToolCallBlock
from pickel.providers.anthropic import AnthropicMessagesProvider
from pickel.providers.gemini import GeminiProvider


def _sample_tool_history_context() -> ModelContext:
    return ModelContext(
        system=SystemContent.from_text("you are pickle"),
        messages=[
            UserMessage(content=[TextBlock(text="list files")]),
            AssistantMessage(
                content=[
                    TextBlock(text="I'll list."),
                    ToolCallBlock(
                        id="c1", name="list_directory", arguments={"path": "."}
                    ),
                    ToolCallBlock(
                        id="c2", name="read_file", arguments={"path": "a.py"}
                    ),
                ]
            ),
            ToolResultMessage(
                tool_call_id="c1",
                tool_name="list_directory",
                content=[TextBlock(text="a.py\nb.py")],
            ),
            ToolResultMessage(
                tool_call_id="c2",
                tool_name="read_file",
                content=[TextBlock(text="print(1)")],
            ),
            AssistantMessage(content=[TextBlock(text="done")]),
        ],
        tools=[
            ToolDefinition(
                name="list_directory",
                description="list",
                input_schema={"type": "object"},
            ),
            ToolDefinition(
                name="read_file",
                description="read",
                input_schema={"type": "object"},
            ),
        ],
    )


class ModelContextGenerateTests(unittest.TestCase):
    def test_both_providers_accept_same_model_context_structure(self) -> None:
        context = ModelContext(
            system=SystemContent.from_text("sys"),
            messages=[UserMessage(content=[TextBlock(text="hi")])],
            tools=[
                ToolDefinition(
                    name="echo",
                    description="echo",
                    input_schema={"type": "object"},
                )
            ],
        )

        anthropic = AnthropicMessagesProvider(model="claude-test")
        anthropic.client = SimpleNamespace(
            messages=SimpleNamespace(
                stream=Mock(
                    return_value=_FakeStreamManager(
                        SimpleNamespace(
                            id="a1",
                            model="claude-test",
                            stop_reason="end_turn",
                            usage=None,
                            content=[SimpleNamespace(type="text", text="ok-a")],
                        )
                    )
                )
            )
        )
        gemini = GeminiProvider(model="gemini-test")
        gemini.client = SimpleNamespace(
            aio=SimpleNamespace(
                models=SimpleNamespace(
                    generate_content=AsyncMock(
                        return_value=SimpleNamespace(
                            candidates=[
                                SimpleNamespace(
                                    content=SimpleNamespace(
                                        parts=[
                                            SimpleNamespace(
                                                text="ok-g", function_call=None
                                            )
                                        ]
                                    ),
                                    finish_message=None,
                                )
                            ],
                            response_id="g1",
                            model_version="v",
                            usage_metadata=None,
                            text="ok-g",
                            function_calls=None,
                        )
                    )
                )
            )
        )

        a_result = asyncio.run(anthropic.generate(context))
        g_result = asyncio.run(gemini.generate(context))
        self.assertIsInstance(a_result, AssistantMessage)
        self.assertIsInstance(g_result, AssistantMessage)
        self.assertEqual("ok-a", a_result.content[0].text)
        self.assertEqual("ok-g", g_result.content[0].text)

    def test_anthropic_multi_round_tool_history_aggregates_tool_results(self) -> None:
        context = _sample_tool_history_context()
        wire = AnthropicMessagesProvider._build_messages(context.messages)
        roles = [item["role"] for item in wire]
        # user, assistant(tool calls), user(tool_results aggregated), assistant(final)
        self.assertEqual(["user", "assistant", "user", "assistant"], roles)
        tool_results = wire[2]["content"]
        self.assertEqual(2, len(tool_results))
        self.assertEqual("tool_result", tool_results[0]["type"])
        self.assertEqual("c1", tool_results[0]["tool_use_id"])
        self.assertEqual("c2", tool_results[1]["tool_use_id"])
        self.assertEqual("tool_use", wire[1]["content"][1]["type"])
        self.assertEqual("tool_use", wire[1]["content"][2]["type"])

    def test_gemini_multi_round_tool_history_function_responses(self) -> None:
        context = _sample_tool_history_context()
        contents = GeminiProvider._build_contents(context.messages)
        self.assertEqual(4, len(contents))
        self.assertEqual("user", contents[0].role)
        self.assertEqual("model", contents[1].role)
        self.assertEqual("user", contents[2].role)
        self.assertEqual("model", contents[3].role)
        # text + two function_calls
        self.assertEqual(3, len(contents[1].parts))
        self.assertEqual(2, len(contents[2].parts))
        self.assertEqual("c1", contents[2].parts[0].function_response.id)
        self.assertEqual("c2", contents[2].parts[1].function_response.id)
        self.assertEqual(
            {
                "content": [{"type": "text", "text": "a.py\nb.py"}],
                "structured_content": None,
                "is_error": False,
            },
            contents[2].parts[0].function_response.response,
        )


class _FakeStream:
    def __init__(self, final_message) -> None:
        self.final_message = final_message

    def __aiter__(self):
        async def _no_events():
            return
            yield  # pragma: no cover

        return _no_events()

    async def get_final_message(self):
        return self.final_message


class _FakeStreamManager:
    def __init__(self, final_message) -> None:
        self.final_message = final_message

    async def __aenter__(self):
        return _FakeStream(self.final_message)

    async def __aexit__(self, exc_type, exc, exc_tb) -> None:
        return None


if __name__ == "__main__":
    unittest.main()
