import asyncio
from datetime import datetime, timezone
from pathlib import Path

import pytest

from pickel.operations.agent_delegation import AgentDelegation
from pickel.shared.execution_identity import ExecutionIdentity
from pickel.tools.base import ToolExecutionContext, ToolExecutionError
from pickel.tools.delegate_agent import delegate_agent
from pickel.tools.services import ToolServices


def _run_async(function):
    def wrapper():
        asyncio.run(function())

    return wrapper


class _DelegationControl:
    def __init__(self) -> None:
        self.calls = []

    async def start_delegation(
        self,
        *,
        parent_operation_id,
        parent_step_id,
        parent_tool_call_id,
        message,
    ):
        self.calls.append(
            (
                parent_operation_id,
                parent_step_id,
                parent_tool_call_id,
                message,
            )
        )
        return AgentDelegation(
            child_session_id="child-session",
            parent_operation_id=parent_operation_id,
            parent_step_id=parent_step_id,
            parent_tool_call_id=parent_tool_call_id,
            initial_message_id="child-message",
            created_at=datetime(2026, 8, 25, tzinfo=timezone.utc),
        )


def _context(control: _DelegationControl) -> ToolExecutionContext:
    return ToolExecutionContext(
        agent_id="Pickle",
        identity=ExecutionIdentity(
            session_id="session-1",
            operation_id="operation-1",
            step_id="step-1",
            tool_call_id="tool-1",
        ),
        workspace_path=Path.cwd(),
        services=ToolServices(delegation=control),
    )


@_run_async
async def test_delegate_agent_returns_durable_acceptance_handle() -> None:
    control = _DelegationControl()

    result = await delegate_agent.execute(
        {"description": "research", "prompt": "Find the answer."},
        _context(control),
    )

    assert result == {
        "child_session_id": "child-session",
        "message_id": "child-message",
    }
    assert len(control.calls) == 1
    operation_id, step_id, tool_call_id, message = control.calls[0]
    assert (operation_id, step_id, tool_call_id) == (
        "operation-1",
        "step-1",
        "tool-1",
    )
    assert message.content[0].text == "Find the answer."


@_run_async
async def test_delegate_agent_requires_delegation_control() -> None:
    with pytest.raises(ToolExecutionError, match="DelegationControl"):
        await delegate_agent.execute(
            {"description": "research", "prompt": "Find the answer."},
            ToolExecutionContext(
                agent_id="Pickle",
                identity=ExecutionIdentity(session_id="session-1"),
                workspace_path=Path.cwd(),
            ),
        )


@_run_async
async def test_delegate_agent_does_not_persist_description_as_title() -> None:
    control = _DelegationControl()

    await delegate_agent.execute(
        {"description": "A display-only label", "prompt": "Do the work."},
        _context(control),
    )

    assert control.calls[0][3].content[0].text == "Do the work."
