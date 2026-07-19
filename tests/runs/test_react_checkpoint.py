"""ReAct checkpoint 落盘顺序：user → assistant intent → tool results → final."""

from __future__ import annotations

import asyncio
import unittest
from dataclasses import dataclass, field
from pathlib import Path

from myopenclaw.agents.agent import Agent
from myopenclaw.context.assembler import ContextAssembler
from myopenclaw.conversations.agent_message import AssistantMessage, UserMessage
from myopenclaw.conversations.content_blocks import TextContent, ToolCallContent
from myopenclaw.conversations.session import Session
from myopenclaw.conversations.session_entry import SessionEntry
from myopenclaw.runs.dependencies import RunDependencies
from myopenclaw.runs.strategy.react import ReActStrategy
from myopenclaw.shared.model_config import ModelConfig
from myopenclaw.tools.base import BaseTool, ToolExecutionContext, ToolExecutionResult, ToolSpec


class FakeProvider:
    def __init__(self, responses: list[AssistantMessage]) -> None:
        self.responses = list(responses)
        self.calls = 0

    async def generate(self, context):
        idx = min(self.calls, len(self.responses) - 1)
        self.calls += 1
        return self.responses[idx]


class EchoTool(BaseTool):
    def __init__(self) -> None:
        self.spec = ToolSpec(
            name="echo",
            description="echo",
            input_schema={"type": "object", "properties": {"text": {"type": "string"}}},
        )

    async def execute(self, arguments, context: ToolExecutionContext) -> ToolExecutionResult:
        return ToolExecutionResult(content=str(arguments.get("text", "")))


@dataclass
class SpySessionService:
    flushes: list[list[str]] = field(default_factory=list)

    def flush_new_entries(self, *, session: Session, entries: list[SessionEntry]) -> None:
        self.flushes.append([e.entry_id for e in entries])


class ReactCheckpointTests(unittest.TestCase):
    def _agent(self) -> Agent:
        return Agent(
            agent_id="Pickle",
            workspace_path=Path("."),
            behavior_path=Path("."),
            behavior_instruction="you are pickle",
            model_config=ModelConfig(provider="fake", model="fake"),
            tool_ids=["echo"],
        )

    def test_checkpoint_order_user_assistant_tools_final(self) -> None:
        session = Session.create(agent_id="Pickle")
        # user already appended by coordinator in real path; simulate here
        session.append_user(UserMessage(content=[TextContent(text="hi")]))

        provider = FakeProvider(
            [
                AssistantMessage(
                    content=[
                        TextContent(text="calling"),
                        ToolCallContent(
                            id="c1", name="echo", arguments={"text": "pong"}
                        ),
                    ]
                ),
                AssistantMessage(content=[TextContent(text="done")]),
            ]
        )
        spy = SpySessionService()
        deps = RunDependencies(
            agent=self._agent(),
            provider=provider,  # type: ignore[arg-type]
            tools=[EchoTool()],
            context_assembler=ContextAssembler(),
            session_service=spy,  # type: ignore[arg-type]
            unit_window=5,
        )
        final = asyncio.run(ReActStrategy(max_steps=4).execute(deps=deps, session=session))
        self.assertEqual("done", final.content[0].text)

        roles = []
        for entry in session.active_path():
            payload = entry.payload
            roles.append(payload.get("role"))
        # user, assistant(tool), tool, assistant(final)
        self.assertEqual(["user", "assistant", "tool", "assistant"], roles)

        # assistant intent flushed before tool result
        # flushes: assistant intent, tool result, final assistant
        self.assertGreaterEqual(len(spy.flushes), 3)
        # first flush after user is assistant with tool call
        first_assistant = session.entries[1]
        self.assertEqual("assistant", first_assistant.payload.get("role"))
        contents = first_assistant.payload.get("content") or []
        self.assertTrue(any(c.get("type") == "tool_call" for c in contents if isinstance(c, dict)))


if __name__ == "__main__":
    unittest.main()
