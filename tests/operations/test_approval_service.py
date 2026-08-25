"""ApprovalService 的查询、顺序和 revision CAS 合同。"""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from pickel.operations.agent_run_state import (
    AgentRunState,
    Cancellation,
    ModelStepState,
    ToolApproval,
    ToolApprovalDecision,
    ToolCallState,
)
from pickel.operations.approval_service import ApprovalService
from pickel.persistence.errors import StorageConflictError

NOW = datetime(2026, 8, 25, tzinfo=timezone.utc)


class _Operations:
    def __init__(self, state: AgentRunState, *, commit: bool = True) -> None:
        self.state = state
        self.commit_enabled = commit

    def load_agent_run_state(self, _operation_id: str) -> AgentRunState:
        return self.state

    def load_operation(self, _operation_id: str):
        return SimpleNamespace(session_id="session-1")

    def commit_transition(
        self, *, state: AgentRunState, expected_revision: int, node, updated_at
    ) -> bool:
        if not self.commit_enabled or expected_revision != self.state.revision:
            return False
        self.state = state
        return True


def _call(
    tool_call_id: str,
    *,
    status: str = "waiting_approval",
    decision: ToolApprovalDecision | None = None,
) -> ToolCallState:
    return ToolCallState(
        tool_call_id=tool_call_id,
        tool_name="echo",
        arguments={},
        status=status,  # type: ignore[arg-type]
        approval=ToolApproval(NOW, "tool_policy", "needs approval", decision),
        replay_policy="safe",
        execution_intent=None,
        decision_reason=None,
        result_node_id=None,
        is_error=None,
    )


def _state(*calls: ToolCallState, status: str = "waiting", revision: int = 7):
    return AgentRunState(
        operation_id="operation-1",
        revision=revision,
        status=status,  # type: ignore[arg-type]
        waiting_reason="tool_approval" if status == "waiting" else None,
        completed_step_count=0,
        current_step=ModelStepState(
            step_id="step-1",
            step_sequence=1,
            phase="awaiting_tools",
            request_attempt=0,
            request_intent=None,
            assistant_message_node_id="assistant-1",
            tool_calls=tuple(calls),
        ),
        final_assistant_node_id=None,
        error=None,
        cancellation=None,
    )


def _service(
    *calls: ToolCallState,
    revision: int = 7,
    status: str = "waiting",
    wake=None,
):
    operations = _Operations(_state(*calls, revision=revision, status=status))
    return (
        ApprovalService(
            operations,
            wake=wake or (lambda _session_id: None),
            now=lambda: NOW,
        ),
        operations,
    )


def test_list_pending_approvals_keeps_provider_order() -> None:
    service, _operations = _service(_call("tool-1"), _call("tool-2"))

    assert tuple(
        item.tool_call_id for item in service.list_pending_approvals("operation-1")
    ) == (
        "tool-1",
        "tool-2",
    )


def test_decide_one_of_multiple_approvals_keeps_waiting_state() -> None:
    service, operations = _service(_call("tool-1"), _call("tool-2"))

    state = service.submit_tool_approval(
        "operation-1",
        "step-1",
        "tool-1",
        outcome="approved",
        expected_revision=7,
        actor_id="operator",
        reason="safe",
    )

    assert state.status == "waiting"
    assert state.waiting_reason == "tool_approval"
    assert [call.status for call in state.current_step.tool_calls] == [
        "ready",
        "waiting_approval",
    ]
    assert operations.state.revision == 8


def test_decide_last_approval_resumes_running_and_duplicate_is_idempotent() -> None:
    woken: list[str] = []
    service, _operations = _service(_call("tool-1"), wake=woken.append)

    state = service.submit_tool_approval(
        "operation-1",
        "step-1",
        "tool-1",
        outcome="denied",
        expected_revision=7,
        actor_id="operator",
        reason="not allowed",
    )
    duplicate = service.submit_tool_approval(
        "operation-1",
        "step-1",
        "tool-1",
        outcome="denied",
        expected_revision=7,
        actor_id="operator",
        reason="not allowed",
    )

    assert state.status == "running"
    assert state.waiting_reason is None
    assert state.current_step.tool_calls[0].status == "denied"
    assert duplicate == state
    assert woken == ["session-1"]


def test_stale_revision_and_conflicting_decision_are_rejected() -> None:
    service, _operations = _service(_call("tool-1"), _call("tool-2"))
    service.submit_tool_approval(
        "operation-1",
        "step-1",
        "tool-1",
        outcome="approved",
        expected_revision=7,
        actor_id="operator",
        reason="safe",
    )

    with pytest.raises(StorageConflictError):
        service.submit_tool_approval(
            "operation-1",
            "step-1",
            "tool-2",
            outcome="denied",
            expected_revision=7,
            actor_id="operator",
            reason="unsafe",
        )
    with pytest.raises(StorageConflictError):
        service.submit_tool_approval(
            "operation-1",
            "step-1",
            "tool-1",
            outcome="denied",
            expected_revision=8,
            actor_id="operator",
            reason="safe",
        )


def test_stale_step_identity_is_rejected() -> None:
    service, _operations = _service(_call("tool-1"))

    with pytest.raises(StorageConflictError, match="step_id"):
        service.submit_tool_approval(
            "operation-1",
            "old-step",
            "tool-1",
            outcome="approved",
            expected_revision=7,
        )


@pytest.mark.parametrize("status", ["cancelling", "cancelled", "succeeded", "failed"])
def test_cancelled_or_terminal_operation_rejects_approval(status: str) -> None:
    service, _operations = _service(_call("tool-1"))
    if status in {"cancelling", "cancelled"}:
        # The helper's normal state has no Cancellation; construct the required form.
        service._operations.state = AgentRunState(
            operation_id="operation-1",
            revision=7,
            status=status,  # type: ignore[arg-type]
            waiting_reason=None,
            completed_step_count=0,
            current_step=(
                service._operations.state.current_step
                if status == "cancelling"
                else None
            ),
            final_assistant_node_id=None,
            error=None,
            cancellation=Cancellation("user", NOW),
        )
    elif status == "succeeded":
        service._operations.state = AgentRunState(
            "operation-1", 7, "succeeded", None, 0, None, "assistant-1", None, None
        )
    else:
        from pickel.operations.agent_run_state import AgentRunError

        service._operations.state = AgentRunState(
            "operation-1",
            7,
            "failed",
            None,
            0,
            None,
            None,
            AgentRunError("failed", "failed", False),
            None,
        )
    with pytest.raises(StorageConflictError):
        service.submit_tool_approval(
            "operation-1",
            "step-1",
            "tool-1",
            outcome="approved",
            expected_revision=7,
        )


def test_storage_cas_failure_is_not_retried() -> None:
    operations = _Operations(_state(_call("tool-1")), commit=False)
    service = ApprovalService(
        operations, wake=lambda _session_id: None, now=lambda: NOW
    )

    with pytest.raises(StorageConflictError):
        service.submit_tool_approval(
            "operation-1",
            "step-1",
            "tool-1",
            outcome="approved",
            expected_revision=7,
        )
    assert operations.state.revision == 7


def test_cancel_winning_the_same_revision_rejects_late_approval() -> None:
    class CancelWins(_Operations):
        def commit_transition(
            self, *, state: AgentRunState, expected_revision: int, node, updated_at
        ) -> bool:
            self.state = AgentRunState(
                operation_id=self.state.operation_id,
                revision=expected_revision + 1,
                status="cancelling",
                waiting_reason=None,
                completed_step_count=self.state.completed_step_count,
                current_step=None,
                final_assistant_node_id=None,
                error=None,
                cancellation=Cancellation("user", NOW),
            )
            return False

    operations = CancelWins(_state(_call("tool-1")))
    service = ApprovalService(
        operations, wake=lambda _session_id: None, now=lambda: NOW
    )

    with pytest.raises(StorageConflictError, match="CAS"):
        service.submit_tool_approval(
            "operation-1",
            "step-1",
            "tool-1",
            outcome="approved",
            expected_revision=7,
        )
    assert operations.state.status == "cancelling"
    assert operations.state.revision == 8
