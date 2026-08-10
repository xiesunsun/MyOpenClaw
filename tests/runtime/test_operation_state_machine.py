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


def test_state_machine_drives_tool_call_through_persisted_intent() -> None:
    machine = OperationStateMachine()
    state = _state(revision=1, status="queued")

    assert machine.decide_next_action(state).action == "start_model_step"
    state = machine.start_model_step(state, step_id="step-1")
    assert machine.decide_next_action(state).action == "record_model_request_intent"
    state = machine.record_model_request_intent(state)
    assert machine.decide_next_action(state).action == "execute_model_request"
    state = machine.record_model_request_completed(
        state,
        assistant_message_node_id="assistant-node",
    )
    state = machine.prepare_tool_calls(
        state,
        tool_calls=(
            ToolCallState(
                tool_call_id="tool-1",
                tool_name="echo",
                arguments={"text": "hello"},
                execution_state="ready",
            ),
        ),
    )
    decision = machine.decide_next_action(state)
    assert decision.action == "record_tool_call_intent"
    assert decision.tool_call_id == "tool-1"

    state = machine.record_tool_call_intent(state, tool_call_id="tool-1")
    assert machine.decide_next_action(state).action == "execute_tool_call"
    state = machine.record_tool_call_completed(
        state,
        tool_call_id="tool-1",
        result_message_node_id="result-node",
        is_error=False,
    )
    assert machine.decide_next_action(state).action == "invoke_post_tool_batch_hook"
    state = machine.record_post_tool_batch_hook_completed(state)
    state = machine.complete_model_step(state)
    state = machine.finish_agent_run(
        state,
        final_assistant_node_id="assistant-node",
    )

    assert state.status == "succeeded"
    assert state.current_step is None
    assert state.completed_step_ids == ("step-1",)
    assert machine.decide_next_action(state).action == "done"


def test_prepare_no_tools_completes_model_step() -> None:
    machine = OperationStateMachine()
    state = _state(
        revision=3,
        step=_step("model_request_intent_recorded"),
    )
    state = machine.record_model_request_completed(
        state,
        assistant_message_node_id="assistant-node",
    )

    state = machine.prepare_tool_calls(state, tool_calls=())

    assert state.current_step is not None
    assert state.current_step.phase == "completed"
    assert machine.decide_next_action(state).action == "finish_agent_run"


def test_completed_tool_step_is_archived_before_next_model_step() -> None:
    machine = OperationStateMachine()
    completed_call = ToolCallState(
        tool_call_id="tool-1",
        tool_name="echo",
        arguments={},
        execution_state="completed",
        result_message_node_id="result-node",
        is_error=False,
    )
    state = _state(
        revision=8,
        step=_step("completed", tool_calls=(completed_call,)),
    )

    assert machine.decide_next_action(state).action == "archive_model_step"
    archived = machine.archive_completed_model_step(state)

    assert archived.current_step is None
    assert archived.completed_step_ids == ("step-1",)
    assert machine.decide_next_action(archived).action == "start_model_step"


def test_unknown_tool_effect_can_transition_to_waiting() -> None:
    machine = OperationStateMachine()
    intent = ToolCallState(
        tool_call_id="tool-1",
        tool_name="external",
        arguments={},
        execution_state="intent_recorded",
    )
    state = _state(
        revision=6,
        step=_step("tool_calls_running", tool_calls=(intent,)),
    )

    waiting = machine.pause_for_unknown_tool_effect(state)

    assert waiting.status == "waiting"
    assert waiting.current_step == state.current_step
    assert machine.decide_next_action(waiting).action == "pause"
