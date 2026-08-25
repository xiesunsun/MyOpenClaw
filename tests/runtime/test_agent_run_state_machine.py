from __future__ import annotations

import pytest

from datetime import datetime, timezone

from pickel.operations.agent_run_state import (
    AgentRunError,
    AgentRunState,
    DelegateAgentIntent,
    ModelRequestIntent,
    ModelStepState,
    ToolApproval,
    ToolApprovalDecision,
    ToolCallState,
)
from pickel.context.model_context import ModelContext, SystemContent
from pickel.persistence.errors import StorageIntegrityError
from pickel.runtime.agent_run_state_machine import AgentRunStateMachine


def _state(
    revision: int,
    status: str = "queued",
    *,
    error: AgentRunError | None = None,
) -> AgentRunState:
    return AgentRunState(
        "operation-1",
        revision,
        status,  # type: ignore[arg-type]
        None,
        0,
        None,
        None,
        error,
        None,
    )


def test_create_queued_is_the_only_initial_shape() -> None:
    state = AgentRunStateMachine().create_queued("operation-1")
    assert state == _state(1)


def test_status_transition_requires_consecutive_revision() -> None:
    machine = AgentRunStateMachine()
    with pytest.raises(StorageIntegrityError, match="revision"):
        machine.validate_transition(current=_state(1), next_state=_state(3, "running"))
    machine.validate_transition(current=_state(1), next_state=_state(2, "running"))


def test_terminal_state_cannot_restart_and_must_clear_step() -> None:
    machine = AgentRunStateMachine()
    failed = _state(
        2,
        "failed",
        error=AgentRunError("runtime", "failed", retryable=True),
    )
    with pytest.raises(StorageIntegrityError, match="status"):
        machine.validate_transition(current=failed, next_state=_state(3, "running"))


def test_failed_state_requires_error_from_running() -> None:
    machine = AgentRunStateMachine()
    with pytest.raises(ValueError, match="failed 状态必须有 error"):
        _state(2, "failed")
    machine.validate_transition(
        current=_state(1, "running"),
        next_state=_state(
            2,
            "failed",
            error=AgentRunError("runtime", "failed", retryable=True),
        ),
    )


def test_failed_transition_can_clear_incomplete_step_without_counting_it() -> None:
    machine = AgentRunStateMachine()

    current = AgentRunState(
        "operation-1",
        1,
        "running",
        None,
        0,
        ModelStepState("step-1", 1, "preparing_request", 0, None, None, ()),
        None,
        None,
        None,
    )
    failed = AgentRunState(
        "operation-1",
        2,
        "failed",
        None,
        0,
        None,
        None,
        AgentRunError("runtime", "failed", retryable=True),
        None,
    )
    machine.validate_transition(current=current, next_state=failed)


def test_step_must_start_at_preparing_request() -> None:
    machine = AgentRunStateMachine()

    current = _state(1, "running")
    with pytest.raises(ValueError, match="request_ready 必须有 request_intent"):
        AgentRunState(
            "operation-1",
            2,
            "running",
            None,
            0,
            ModelStepState("step-1", 1, "request_ready", 0, None, None, ()),
            None,
            None,
            None,
        )
    # Entity-level validation rejects the malformed request phase before the
    # state machine sees it; a valid first step is accepted by the machine.
    valid = AgentRunState(
        "operation-1",
        2,
        "running",
        None,
        0,
        ModelStepState("step-1", 1, "preparing_request", 0, None, None, ()),
        None,
        None,
        None,
    )
    machine.validate_transition(current=current, next_state=valid)


def test_request_intent_is_frozen_and_attempt_only_increments_once() -> None:
    machine = AgentRunStateMachine()
    intent = ModelRequestIntent(ModelContext(SystemContent(), ()), "fp-1")
    current = AgentRunState(
        "operation-1",
        1,
        "running",
        None,
        0,
        ModelStepState("step-1", 1, "request_ready", 0, intent, None, ()),
        None,
        None,
        None,
    )
    retry = AgentRunState(
        "operation-1",
        2,
        "running",
        None,
        0,
        ModelStepState("step-1", 1, "request_ready", 1, intent, None, ()),
        None,
        None,
        None,
    )
    machine.validate_transition(current=current, next_state=retry)
    skipped = AgentRunState(
        "operation-1",
        2,
        "running",
        None,
        0,
        ModelStepState("step-1", 1, "request_ready", 2, intent, None, ()),
        None,
        None,
        None,
    )
    with pytest.raises(StorageIntegrityError, match="request_attempt"):
        machine.validate_transition(current=current, next_state=skipped)
    changed = AgentRunState(
        "operation-1",
        2,
        "running",
        None,
        0,
        ModelStepState(
            "step-1",
            1,
            "request_ready",
            0,
            ModelRequestIntent(
                ModelContext(SystemContent.from_text("changed"), ()), "fp-2"
            ),
            None,
            (),
        ),
        None,
        None,
        None,
    )
    with pytest.raises(StorageIntegrityError, match="Intent"):
        machine.validate_transition(current=current, next_state=changed)


def test_ordinary_tool_intent_may_be_empty() -> None:
    machine = AgentRunStateMachine()
    current = _running_with_step(1, _awaiting_tools(status="ready"))
    next_state = _running_with_step(2, _awaiting_tools(status="intent_recorded"))
    machine.validate_transition(current=current, next_state=next_state)


def _awaiting_tools(
    *,
    status: str = "ready",
    approval: ToolApproval | None = None,
    execution_intent=None,
    result_node_id: str | None = None,
    is_error: bool | None = None,
) -> ModelStepState:
    return ModelStepState(
        "step-1",
        1,
        "awaiting_tools",
        0,
        None,
        "assistant-1",
        (
            ToolCallState(
                "tool-1",
                "echo",
                {},
                status,  # type: ignore[arg-type]
                approval,
                "safe",
                execution_intent,
                None,
                result_node_id,
                is_error,
            ),
        ),
    )


def _running_with_step(revision: int, step: ModelStepState) -> AgentRunState:
    return AgentRunState(
        "operation-1",
        revision,
        "running",
        None,
        0,
        step,
        None,
        None,
        None,
    )


def test_completed_tool_step_can_be_cleared_before_next_model_step() -> None:
    machine = AgentRunStateMachine()
    current = _running_with_step(
        1,
        _awaiting_tools(status="completed", result_node_id="result-1", is_error=False),
    )
    cleared = AgentRunState(
        "operation-1", 2, "running", None, 1, None, None, None, None
    )
    machine.validate_transition(current=current, next_state=cleared)

    next_step = AgentRunState(
        "operation-1",
        3,
        "running",
        None,
        1,
        ModelStepState("step-2", 2, "preparing_request", 0, None, None, ()),
        None,
        None,
        None,
    )
    machine.validate_transition(current=cleared, next_state=next_step)


def test_waiting_requires_matching_recovery_reason_and_tool_state() -> None:
    machine = AgentRunStateMachine()
    approval = ToolApproval(
        datetime(2026, 8, 25, tzinfo=timezone.utc),
        "tool_policy",
        "needs approval",
        None,
    )
    waiting = AgentRunState(
        "operation-1",
        2,
        "waiting",
        "tool_approval",
        0,
        _awaiting_tools(status="waiting_approval", approval=approval),
        None,
        None,
        None,
    )
    request_ready = AgentRunState(
        "operation-1",
        1,
        "running",
        None,
        0,
        ModelStepState(
            "step-1",
            1,
            "request_ready",
            0,
            ModelRequestIntent(ModelContext(SystemContent(), ()), "fp-1"),
            None,
            (),
        ),
        None,
        None,
        None,
    )
    machine.validate_transition(
        current=request_ready,
        next_state=waiting,
    )

    invalid = AgentRunState(
        "operation-1",
        2,
        "waiting",
        "tool_reconciliation",
        0,
        _awaiting_tools(status="waiting_approval", approval=approval),
        None,
        None,
        None,
    )
    with pytest.raises(StorageIntegrityError, match="intent_recorded"):
        machine.validate_transition(current=request_ready, next_state=invalid)

    running_with_waiting_approval = AgentRunState(
        "operation-1",
        3,
        "running",
        None,
        0,
        _awaiting_tools(status="waiting_approval", approval=approval),
        None,
        None,
        None,
    )
    with pytest.raises(StorageIntegrityError, match="必须等待审批"):
        machine.validate_transition(
            current=waiting,
            next_state=running_with_waiting_approval,
        )


@pytest.mark.parametrize(
    "old_status,new_status",
    [
        ("waiting_approval", "intent_recorded"),
        ("ready", "denied"),
        ("denied", "intent_recorded"),
        ("intent_recorded", "ready"),
        ("completed", "intent_recorded"),
    ],
)
def test_tool_call_illegal_transitions_are_rejected(
    old_status: str, new_status: str
) -> None:
    machine = AgentRunStateMachine()
    approval = None
    if old_status == "waiting_approval":
        approval = ToolApproval(
            datetime(2026, 8, 25, tzinfo=timezone.utc),
            "tool_policy",
            "needs approval",
            None,
        )
    elif old_status == "denied":
        approval = ToolApproval(
            datetime(2026, 8, 25, tzinfo=timezone.utc),
            "tool_policy",
            "needs approval",
            ToolApprovalDecision(
                "denied",
                datetime(2026, 8, 25, tzinfo=timezone.utc),
                "test",
                "test",
            ),
        )
    intent = (
        DelegateAgentIntent("package-child")
        if old_status == "intent_recorded"
        else None
    )
    next_intent = (
        DelegateAgentIntent("package-child")
        if new_status == "intent_recorded"
        else None
    )
    next_approval = approval
    if new_status == "denied" and next_approval is None:
        next_approval = ToolApproval(
            datetime(2026, 8, 25, tzinfo=timezone.utc),
            "tool_policy",
            "needs approval",
            ToolApprovalDecision(
                "denied",
                datetime(2026, 8, 25, tzinfo=timezone.utc),
                "test",
                "test",
            ),
        )
    with pytest.raises((StorageIntegrityError, ValueError)):
        old = _running_with_step(
            1,
            _awaiting_tools(
                status=old_status,
                approval=approval,
                execution_intent=intent,
                result_node_id="result-1" if old_status == "completed" else None,
                is_error=False if old_status == "completed" else None,
            ),
        )
        new = _running_with_step(
            2,
            _awaiting_tools(
                status=new_status,
                approval=next_approval,
                execution_intent=next_intent,
                result_node_id="result-1" if new_status == "completed" else None,
                is_error=False if new_status == "completed" else None,
            ),
        )
        machine.validate_transition(current=old, next_state=new)
