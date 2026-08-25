import asyncio
from pathlib import Path

from pickel.shared.execution_identity import ExecutionIdentity
from pickel.tools.base import ToolExecutionContext
from pickel.tools.cancel_delegation import cancel_delegation
from pickel.tools.services import ToolServices


class _Control:
    def __init__(self, operation_id: str | None) -> None:
        self.operation_id = operation_id

    async def cancel_delegation(self, **kwargs):
        assert kwargs == {
            "sender_operation_id": "operation-1",
            "sender_step_id": "step-1",
            "sender_tool_call_id": "tool-cancel",
            "target_child_session_id": "child-1",
        }
        return self.operation_id


def _context(operation_id: str | None) -> ToolExecutionContext:
    return ToolExecutionContext(
        agent_id="parent",
        identity=ExecutionIdentity(
            session_id="session-1",
            operation_id="operation-1",
            step_id="step-1",
            tool_call_id="tool-cancel",
        ),
        workspace_path=Path.cwd(),
        services=ToolServices(delegation=_Control(operation_id)),
    )


def test_cancel_delegation_schema_and_active_result() -> None:
    result = asyncio.run(
        cancel_delegation.execute({"child_session_id": "child-1"}, _context("child-op"))
    )

    assert cancel_delegation.spec.replay_policy == "safe"
    assert result.structured_content == {
        "child_session_id": "child-1",
        "operation_id": "child-op",
        "status": "cancellation_requested",
    }


def test_cancel_delegation_idle_result() -> None:
    result = asyncio.run(
        cancel_delegation.execute({"child_session_id": "child-1"}, _context(None))
    )

    assert result.is_error is False
    assert result.structured_content == {
        "child_session_id": "child-1",
        "operation_id": None,
        "status": "no_active_operation",
    }
