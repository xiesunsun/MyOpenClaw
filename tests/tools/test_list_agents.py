import asyncio
from pathlib import Path

from pickel.operations.delegation_service import ChildAgentSnapshot
from pickel.shared.execution_identity import ExecutionIdentity
from pickel.tools.base import ToolExecutionContext
from pickel.tools.list_agents import list_agents
from pickel.tools.services import ToolServices


class _Control:
    async def list_child_agents(
        self, *, sender_operation_id, sender_step_id, sender_tool_call_id
    ):
        assert (sender_operation_id, sender_step_id, sender_tool_call_id) == (
            "operation-1",
            "step-1",
            "tool-list",
        )
        return (
            ChildAgentSnapshot(
                child_session_id="child-1",
                agent_id="child-agent",
                status="running",
                operation_id="child-operation",
                waiting_reason=None,
                completed_step_count=2,
                final_assistant_node_id=None,
                error=None,
            ),
        )


def test_list_agents_schema_and_result() -> None:
    context = ToolExecutionContext(
        agent_id="parent",
        identity=ExecutionIdentity(
            session_id="session-1",
            operation_id="operation-1",
            step_id="step-1",
            tool_call_id="tool-list",
        ),
        workspace_path=Path.cwd(),
        services=ToolServices(delegation=_Control()),
    )
    result = asyncio.run(list_agents.execute({}, context))

    assert list_agents.spec.replay_policy == "safe"
    assert list_agents.spec.input_schema == {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }
    assert result[0]["status"] == "running"
