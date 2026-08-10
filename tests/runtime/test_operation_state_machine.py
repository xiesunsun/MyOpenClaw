from __future__ import annotations

import pytest

from pickel.operations.agent_run_state import (
    AgentRunState,
    ModelStepState,
    ToolCallState,
)
from pickel.persistence.storage_transaction import StorageIntegrityError
from pickel.runtime.operation_state_machine import OperationStateMachine


def _state(
    *,
    revision: int,
    status: str = "running",
    step: ModelStepState | None = None,
) -> AgentRunState:
    return AgentRunState(
        operation_id="operation-1",
        revision=revision,
        status=status,  # type: ignore[arg-type]
        user_message_node_id="user-node",
        current_step=step,
    )


def _step(
    phase: str,
    *,
    tool_calls: tuple[ToolCallState, ...] = (),
) -> ModelStepState:
    return ModelStepState(
        step_id="step-1",
        step_sequence=1,
        phase=phase,  # type: ignore[arg-type]
        tool_calls=tool_calls,
    )


def test_new_model_step_must_start_ready() -> None:
    machine = OperationStateMachine()
    current = _state(revision=1, status="queued")
    invalid = _state(
        revision=2,
        step=_step("model_request_intent_recorded"),
    )

    with pytest.raises(StorageIntegrityError, match="model_request_ready"):
        machine.validate_agent_run_transition(current=current, next_state=invalid)


def test_model_request_cannot_skip_persisted_intent() -> None:
    machine = OperationStateMachine()
    current = _state(revision=2, step=_step("model_request_ready"))
    invalid = _state(revision=3, step=_step("model_request_completed"))

    with pytest.raises(StorageIntegrityError, match="phase 转换"):
        machine.validate_agent_run_transition(current=current, next_state=invalid)


def test_tool_call_cannot_skip_persisted_intent() -> None:
    machine = OperationStateMachine()
    ready_call = ToolCallState(
        tool_call_id="tool-1",
        tool_name="external_action",
        arguments={"target": "outside"},
        execution_state="ready",
    )
    completed_call = ToolCallState(
        tool_call_id="tool-1",
        tool_name="external_action",
        arguments={"target": "outside"},
        execution_state="completed",
        result_message_node_id="result-node",
        is_error=False,
    )
    current = _state(
        revision=5,
        step=_step("tool_calls_ready", tool_calls=(ready_call,)),
    )
    invalid = _state(
        revision=6,
        step=_step("tool_calls_running", tool_calls=(completed_call,)),
    )

    with pytest.raises(StorageIntegrityError, match="execution_state 转换"):
        machine.validate_agent_run_transition(current=current, next_state=invalid)


def test_tool_call_ready_to_intent_recorded_is_valid() -> None:
    machine = OperationStateMachine()
    ready_call = ToolCallState(
        tool_call_id="tool-1",
        tool_name="external_action",
        arguments={"target": "outside"},
        execution_state="ready",
    )
    intent_call = ToolCallState(
        tool_call_id="tool-1",
        tool_name="external_action",
        arguments={"target": "outside"},
        execution_state="intent_recorded",
    )
    current = _state(
        revision=5,
        step=_step("tool_calls_ready", tool_calls=(ready_call,)),
    )
    next_state = _state(
        revision=6,
        step=_step("tool_calls_running", tool_calls=(intent_call,)),
    )

    machine.validate_agent_run_transition(current=current, next_state=next_state)


def test_terminal_agent_run_cannot_restart() -> None:
    machine = OperationStateMachine()
    current = _state(revision=2, status="failed")
    restarted = _state(revision=3, status="running")

    with pytest.raises(StorageIntegrityError, match="状态转换"):
        machine.validate_agent_run_transition(current=current, next_state=restarted)
