import json
import unittest
from io import StringIO
from pathlib import Path

from pickel.agents.agent import Agent
from pickel.app.runtime import RuntimeConversation
from pickel.app.runtime_models import TurnRequest
from pickel.cli.query import NonInteractiveHostCalls, QuerySurface
from pickel.cli.query_input import QueryInput
from pickel.conversations.agent_message import AssistantMessage, UserMessage
from pickel.conversations.content_blocks import TextContent
from pickel.conversations.session import Session
from pickel.runs.host_call_types import (
    CONFIRMATION_CALL,
    ConfirmationRequest,
    HostCallSource,
)
from pickel.runs.host_calls import HostCallCompleted, HostCallContext
from pickel.runs.runtime_events import AssistantMessageEvent, TurnCompleted, TurnStarted
from pickel.shared.event_envelope import EventEnvelope
from pickel.shared.model_config import ModelConfig


class _Run:
    async def turn(self, *, session, user_message, bus):
        text = "\n".join(block.text for block in user_message.content)
        turn_id = "turn-1"
        await bus.emit(
            TurnStarted(
                envelope=EventEnvelope(session_id=session.session_id, turn_id=turn_id),
                user_text=text,
            )
        )
        session.append_user(user_message)
        reply = AssistantMessage(content=[TextContent(text="完成")])
        session.append_assistant(reply)
        await bus.emit(
            AssistantMessageEvent(
                envelope=EventEnvelope(session_id=session.session_id, turn_id=turn_id),
                text="完成",
            )
        )
        await bus.emit(
            TurnCompleted(
                envelope=EventEnvelope(session_id=session.session_id, turn_id=turn_id),
                elapsed_ms=12,
            )
        )
        return reply


def _conversation() -> RuntimeConversation:
    agent = Agent(
        agent_id="Pickle",
        workspace_path=Path("/tmp/pickel"),
        behavior_path=Path("/tmp/pickel/AGENT.md"),
        behavior_instruction="test",
        model_config=ModelConfig(provider="fake", model="model"),
        tool_ids=[],
    )
    return RuntimeConversation(
        agent=agent,
        run=_Run(),
        session=Session.create(agent_id=agent.agent_id),
    )


class QueryInputTests(unittest.TestCase):
    def test_query_without_stdin_is_plain_user_message(self) -> None:
        message = QueryInput(query="你是谁？").to_user_message()
        self.assertEqual(["你是谁？"], [block.text for block in message.content])

    def test_dash_uses_stdin_as_complete_user_message(self) -> None:
        message = QueryInput(query="-", stdin_text="完整问题").to_user_message()
        self.assertEqual(["完整问题"], [block.text for block in message.content])

    def test_query_and_stdin_keep_task_and_data_as_separate_blocks(self) -> None:
        message = QueryInput(query="总结", stdin_text="日志内容").to_user_message()
        self.assertEqual(
            ["任务：\n总结", "输入数据（stdin）：\n日志内容"],
            [block.text for block in message.content],
        )


class QuerySurfaceTests(unittest.IsolatedAsyncioTestCase):
    async def test_text_mode_prints_only_final_message(self) -> None:
        output = StringIO()
        result = await QuerySurface(stdout=output, output_format="text").run(
            conversation=_conversation(),
            request=TurnRequest(
                message=UserMessage(content=[TextContent(text="开始")])
            ),
        )
        self.assertEqual("completed", result.status)
        self.assertEqual("完成\n", output.getvalue())

    async def test_jsonl_mode_prints_versioned_public_events(self) -> None:
        output = StringIO()
        await QuerySurface(stdout=output, output_format="jsonl").run(
            conversation=_conversation(),
            request=TurnRequest(
                message=UserMessage(content=[TextContent(text="开始")])
            ),
        )
        events = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(
            ["turn.started", "message.completed", "turn.completed"],
            [event["type"] for event in events],
        )
        self.assertTrue(all(event["schema_version"] == 1 for event in events))

    async def test_non_interactive_confirmation_declines(self) -> None:
        conversation = _conversation()
        leases = NonInteractiveHostCalls.attach(conversation)
        try:
            outcome = await conversation.runtime_bus.host_calls.client.call(
                CONFIRMATION_CALL,
                ConfirmationRequest(
                    source=HostCallSource(kind="tool", name="write", operation="write"),
                    title="确认",
                    message="执行写入",
                ),
                HostCallContext(),
            )
        finally:
            for lease in leases:
                lease.close()
        self.assertIsInstance(outcome, HostCallCompleted)
        self.assertEqual("decline", outcome.value.decision)
