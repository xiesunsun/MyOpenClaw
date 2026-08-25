import asyncio
from datetime import datetime, timezone
from pathlib import Path

from pickel.conversations.agent_message import UserMessage
from pickel.conversations.content_blocks import TextBlock
from pickel.inbox.message import AgentMessageSource, InboxMessage
from pickel.shared.execution_identity import ExecutionIdentity
from pickel.tools.base import ToolExecutionContext
from pickel.tools.send_message import send_message
from pickel.tools.services import ToolServices


def _run_async(function):
    def wrapper():
        asyncio.run(function())

    return wrapper


class _Control:
    async def send_parent_followup(
        self,
        *,
        sender_operation_id,
        sender_step_id,
        sender_tool_call_id,
        target_child_session_id,
        message,
    ):
        assert (sender_operation_id, sender_step_id, sender_tool_call_id) == (
            "operation-1",
            "step-1",
            "tool-1",
        )
        assert target_child_session_id == "child-1"
        assert message == UserMessage((TextBlock("continue"),))
        return InboxMessage(
            message_id="message-stable",
            session_id="child-1",
            sequence=2,
            delivery="followup",
            message=message,
            source=AgentMessageSource(
                sender_session_id="session-1",
                sender_operation_id="operation-1",
                form="followup",
            ),
            created_at=datetime(2026, 8, 25, tzinfo=timezone.utc),
        )


def _context() -> ToolExecutionContext:
    return ToolExecutionContext(
        agent_id="parent",
        identity=ExecutionIdentity(
            session_id="session-1",
            operation_id="operation-1",
            step_id="step-1",
            tool_call_id="tool-1",
        ),
        workspace_path=Path.cwd(),
        services=ToolServices(delegation=_Control()),
    )


@_run_async
async def test_send_message_returns_stable_message_handle() -> None:
    result = await send_message.execute(
        {"child_session_id": "child-1", "message": "continue"}, _context()
    )

    assert result.is_error is False
    assert result.structured_content == {"message_id": "message-stable"}
