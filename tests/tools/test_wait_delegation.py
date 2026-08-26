import asyncio
from pathlib import Path

from pickel.conversations.agent_message import AssistantMessage
from pickel.conversations.content_blocks import TextBlock
from pickel.operations.delegation_service import ChildAgentSnapshot
from pickel.shared.execution_identity import ExecutionIdentity
from pickel.tools.base import ToolExecutionContext
from pickel.tools.services import ToolServices
from pickel.tools.wait_delegation import wait_delegation


class _Control:
    async def wait_delegation(self, **kwargs):
        assert kwargs == {
            "sender_operation_id": "operation-1",
            "sender_step_id": "step-1",
            "sender_tool_call_id": "tool-wait",
            "target_child_session_id": "child-1",
            "timeout_seconds": 5.0,
        }
        return (
            ChildAgentSnapshot(
                child_session_id="child-1",
                agent_id="Pickle",
                status="succeeded",
                operation_id="child-operation",
                waiting_reason=None,
                completed_step_count=1,
                final_assistant_node_id="assistant-1",
                error=None,
            ),
            AssistantMessage(content=(TextBlock("final report"),)),
            False,
        )


def test_wait_delegation_returns_persisted_assistant_message() -> None:
    context = ToolExecutionContext(
        agent_id="Pickle",
        identity=ExecutionIdentity(
            session_id="session-1",
            operation_id="operation-1",
            step_id="step-1",
            tool_call_id="tool-wait",
        ),
        workspace_path=Path.cwd(),
        services=ToolServices(delegation=_Control()),
    )

    result = asyncio.run(
        wait_delegation.execute(
            {"child_session_id": "child-1", "timeout_seconds": 5}, context
        )
    )

    assert result.is_error is False
    assert result.structured_content["timed_out"] is False
    assert result.structured_content["agent"]["status"] == "succeeded"
    assert result.structured_content["assistant_message"]["content"] == [
        {"type": "text", "text": "final report"}
    ]
