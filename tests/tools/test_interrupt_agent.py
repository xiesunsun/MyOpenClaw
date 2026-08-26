import asyncio
from pathlib import Path

from pickel.shared.execution_identity import ExecutionIdentity
from pickel.tools.base import ToolExecutionContext
from pickel.tools.interrupt_agent import interrupt_agent
from pickel.tools.services import ToolServices


class _Control:
    async def interrupt_agent(self, **kwargs):
        assert kwargs == {
            "sender_operation_id": "operation-1",
            "sender_step_id": "step-1",
            "sender_tool_call_id": "tool-interrupt",
            "target_child_session_id": "child-1",
        }
        return "child-operation"


def test_interrupt_agent_returns_cancellation_handle() -> None:
    context = ToolExecutionContext(
        agent_id="parent",
        identity=ExecutionIdentity(
            session_id="session-1",
            operation_id="operation-1",
            step_id="step-1",
            tool_call_id="tool-interrupt",
        ),
        workspace_path=Path.cwd(),
        services=ToolServices(delegation=_Control()),
    )

    result = asyncio.run(
        interrupt_agent.execute({"child_session_id": "child-1"}, context)
    )

    assert interrupt_agent.spec.replay_policy == "safe"
    assert interrupt_agent.spec.output_schema is not None
    assert result.structured_content == {
        "child_session_id": "child-1",
        "operation_id": "child-operation",
        "status": "cancellation_requested",
    }
