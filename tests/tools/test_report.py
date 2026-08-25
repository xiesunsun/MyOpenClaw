import asyncio
from datetime import datetime, timezone
from pathlib import Path

from pickel.conversations.agent_message import UserMessage
from pickel.conversations.content_blocks import TextBlock
from pickel.inbox.message import AgentMessageSource, InboxMessage
from pickel.shared.execution_identity import ExecutionIdentity
from pickel.tools.base import ToolExecutionContext
from pickel.tools.report import report
from pickel.tools.services import ToolServices


class _Control:
    async def send_child_report(
        self, *, sender_operation_id, sender_step_id, sender_tool_call_id, output
    ):
        assert (sender_operation_id, sender_step_id, sender_tool_call_id, output) == (
            "child-operation",
            "report-step",
            "report-tool",
            "finding",
        )
        return InboxMessage(
            message_id="message-report",
            session_id="parent-session",
            sequence=2,
            delivery="steer",
            message=UserMessage(
                (TextBlock("Background subagent child-session reported:\nfinding"),)
            ),
            source=AgentMessageSource(
                sender_session_id="child-session",
                sender_operation_id="child-operation",
                form="steer",
            ),
            created_at=datetime(2026, 8, 25, tzinfo=timezone.utc),
        )


def test_report_schema_and_result() -> None:
    context = ToolExecutionContext(
        agent_id="child-agent",
        identity=ExecutionIdentity(
            session_id="child-session",
            operation_id="child-operation",
            step_id="report-step",
            tool_call_id="report-tool",
        ),
        workspace_path=Path.cwd(),
        services=ToolServices(delegation=_Control()),
    )
    result = asyncio.run(report.execute({"output": "finding"}, context))

    assert report.spec.replay_policy == "safe"
    assert report.spec.input_schema["required"] == ["output"]
    assert set(report.spec.input_schema["properties"]) == {"output"}
    assert result.is_error is False
    assert result.structured_content == {"message_id": "message-report"}
    assert "message-report" in result.content
