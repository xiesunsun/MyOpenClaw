"""SessionOperationState 的纯转换校验。"""

from __future__ import annotations

from pickel.operations.agent_run_state import (
    AgentRunState,
    ModelStepState,
    ToolCallState,
)
from pickel.persistence.storage_transaction import StorageIntegrityError


class OperationStateMachine:
    """只校验状态转换；不读写 Store，也不执行外部副作用。"""

    _ALLOWED_RUN_STATUS_TRANSITIONS = {
        "queued": {"running", "failed", "cancelled"},
        "running": {
            "running",
            "waiting",
            "succeeded",
            "failed",
            "cancelled",
        },
        "waiting": {"running", "failed", "cancelled"},
        "succeeded": set(),
        "failed": set(),
        "cancelled": set(),
    }
    _ALLOWED_STEP_PHASE_TRANSITIONS = {
        "model_request_ready": {"model_request_intent_recorded"},
        "model_request_intent_recorded": {
            "model_request_retry_scheduled",
            "model_request_completed",
        },
        "model_request_retry_scheduled": {"model_request_intent_recorded"},
        "model_request_completed": {"tool_calls_ready", "completed"},
        "tool_calls_ready": {"tool_calls_running"},
        "tool_calls_running": {"tool_calls_running", "completed"},
        "completed": set(),
    }
    _ALLOWED_TOOL_CALL_TRANSITIONS = {
        "ready": {"intent_recorded"},
        "intent_recorded": {"completed"},
        "completed": {"completed"},
    }

    def create_initial_agent_run_state(
        self,
        *,
        operation_id: str,
        user_message_node_id: str,
    ) -> AgentRunState:
        return AgentRunState(
            operation_id=operation_id,
            revision=1,
            status="queued",
            user_message_node_id=user_message_node_id,
        )

    def validate_agent_run_transition(
        self,
        *,
        current: AgentRunState,
        next_state: AgentRunState,
    ) -> None:
        self._validate_identity_and_revision(current=current, next_state=next_state)
        if (
            next_state.status
            not in self._ALLOWED_RUN_STATUS_TRANSITIONS[current.status]
        ):
            raise StorageIntegrityError(
                "非法 AgentRunState 状态转换: "
                f"{current.status} -> {next_state.status}"
            )
        self._validate_completed_steps(current=current, next_state=next_state)
        self._validate_current_step(current=current, next_state=next_state)

    @staticmethod
    def _validate_identity_and_revision(
        *,
        current: AgentRunState,
        next_state: AgentRunState,
    ) -> None:
        if next_state.operation_id != current.operation_id:
            raise StorageIntegrityError("AgentRunState.operation_id 不能改变")
        if next_state.user_message_node_id != current.user_message_node_id:
            raise StorageIntegrityError("AgentRunState.user_message_node_id 不能改变")
        if next_state.revision != current.revision + 1:
            raise StorageIntegrityError(
                "AgentRunState.revision 必须连续递增: "
                f"expected={current.revision + 1}, actual={next_state.revision}"
            )

    @staticmethod
    def _validate_completed_steps(
        *,
        current: AgentRunState,
        next_state: AgentRunState,
    ) -> None:
        current_ids = current.completed_step_ids
        next_ids = next_state.completed_step_ids
        if next_ids[: len(current_ids)] != current_ids:
            raise StorageIntegrityError("completed_step_ids 只能在尾部追加")
        if len(next_ids) - len(current_ids) > 1:
            raise StorageIntegrityError("一次状态转换最多完成一个 ModelStep")

    def _validate_current_step(
        self,
        *,
        current: AgentRunState,
        next_state: AgentRunState,
    ) -> None:
        current_step = current.current_step
        next_step = next_state.current_step
        if current_step is None:
            if next_step is None:
                return
            if next_step.step_sequence != len(current.completed_step_ids) + 1:
                raise StorageIntegrityError("新 ModelStep.step_sequence 不连续")
            if next_step.phase != "model_request_ready":
                raise StorageIntegrityError(
                    "新 ModelStep 必须从 model_request_ready 开始"
                )
            return
        if next_step is None:
            if current_step.phase != "completed":
                raise StorageIntegrityError("未完成的 ModelStep 不能被清空")
            if not next_state.completed_step_ids or (
                next_state.completed_step_ids[-1] != current_step.step_id
            ):
                raise StorageIntegrityError(
                    "清空 ModelStep 时必须追加其 step_id 到 completed_step_ids"
                )
            return
        if next_step.step_id != current_step.step_id:
            raise StorageIntegrityError(
                "必须先完成并清空当前 ModelStep，才能创建下一步"
            )
        if next_step.step_sequence != current_step.step_sequence:
            raise StorageIntegrityError("ModelStep.step_sequence 不能改变")
        if (
            next_step.phase
            not in self._ALLOWED_STEP_PHASE_TRANSITIONS[current_step.phase]
        ):
            raise StorageIntegrityError(
                "非法 ModelStepState.phase 转换: "
                f"{current_step.phase} -> {next_step.phase}"
            )
        self._validate_tool_calls(current=current_step, next_state=next_step)

    def _validate_tool_calls(
        self,
        *,
        current: ModelStepState,
        next_state: ModelStepState,
    ) -> None:
        if not current.tool_calls:
            if next_state.tool_calls and next_state.phase != "tool_calls_ready":
                raise StorageIntegrityError(
                    "ToolCallState 必须在 tool_calls_ready 阶段首次出现"
                )
            return
        if len(next_state.tool_calls) != len(current.tool_calls):
            raise StorageIntegrityError("ToolCallState 列表创建后长度不能改变")
        for current_call, next_call in zip(
            current.tool_calls,
            next_state.tool_calls,
            strict=True,
        ):
            self._validate_tool_call(current=current_call, next_state=next_call)

    def _validate_tool_call(
        self,
        *,
        current: ToolCallState,
        next_state: ToolCallState,
    ) -> None:
        if (
            next_state.tool_call_id != current.tool_call_id
            or next_state.tool_name != current.tool_name
            or next_state.arguments != current.arguments
        ):
            raise StorageIntegrityError("ToolCallState 身份和参数不能改变")
        if (
            next_state.execution_state
            not in self._ALLOWED_TOOL_CALL_TRANSITIONS[current.execution_state]
        ):
            raise StorageIntegrityError(
                "非法 ToolCallState.execution_state 转换: "
                f"{current.execution_state} -> {next_state.execution_state}"
            )
