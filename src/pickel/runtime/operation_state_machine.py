"""SessionOperationState 的纯转换校验。"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Literal

from pickel.operations.agent_run_state import (
    AgentRunState,
    ModelStepState,
    ToolCallState,
)
from pickel.persistence.storage_transaction import StorageIntegrityError

OperationAction = Literal[
    "start_model_step",
    "record_model_request_intent",
    "execute_model_request",
    "prepare_tool_calls",
    "complete_model_step",
    "archive_model_step",
    "record_tool_call_intent",
    "execute_tool_call",
    "invoke_post_tool_batch_hook",
    "finish_agent_run",
    "pause",
    "done",
]


@dataclass(frozen=True)
class OperationDecision:
    action: OperationAction
    tool_call_id: str | None = None


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
        "intent_recorded": {"intent_recorded", "completed"},
        "completed": {"completed"},
    }

    def decide_next_action(self, state: AgentRunState) -> OperationDecision:
        """只根据已持久化状态决定下一动作，不执行副作用。"""
        if state.status in {"succeeded", "failed", "cancelled"}:
            return OperationDecision("done")
        if state.status == "waiting":
            return OperationDecision("pause")
        step = state.current_step
        if step is None:
            return OperationDecision("start_model_step")
        if step.phase in {"model_request_ready", "model_request_retry_scheduled"}:
            return OperationDecision("record_model_request_intent")
        if step.phase == "model_request_intent_recorded":
            return OperationDecision("execute_model_request")
        if step.phase == "model_request_completed":
            return OperationDecision("prepare_tool_calls")
        if step.phase in {"tool_calls_ready", "tool_calls_running"}:
            for tool_call in step.tool_calls:
                if tool_call.execution_state == "intent_recorded":
                    return OperationDecision(
                        "execute_tool_call",
                        tool_call_id=tool_call.tool_call_id,
                    )
                if tool_call.execution_state == "ready":
                    return OperationDecision(
                        "record_tool_call_intent",
                        tool_call_id=tool_call.tool_call_id,
                    )
            return OperationDecision(
                "complete_model_step"
                if step.post_tool_batch_hook_completed
                else "invoke_post_tool_batch_hook"
            )
        if step.phase == "completed":
            return OperationDecision(
                "archive_model_step" if step.tool_calls else "finish_agent_run"
            )
        raise StorageIntegrityError(f"无法决定 AgentRun 下一动作: {step.phase}")

    def start_model_step(
        self,
        state: AgentRunState,
        *,
        step_id: str,
    ) -> AgentRunState:
        next_state = replace(
            state,
            revision=state.revision + 1,
            status="running",
            current_step=ModelStepState(
                step_id=step_id,
                step_sequence=len(state.completed_step_ids) + 1,
                phase="model_request_ready",
            ),
        )
        return self._validated(state, next_state)

    def record_model_request_intent(self, state: AgentRunState) -> AgentRunState:
        step = self._require_step(
            state,
            phase=("model_request_ready", "model_request_retry_scheduled"),
        )
        next_state = replace(
            state,
            revision=state.revision + 1,
            current_step=replace(step, phase="model_request_intent_recorded"),
        )
        return self._validated(state, next_state)

    def record_model_request_completed(
        self,
        state: AgentRunState,
        *,
        assistant_message_node_id: str,
    ) -> AgentRunState:
        step = self._require_step(state, phase="model_request_intent_recorded")
        next_state = replace(
            state,
            revision=state.revision + 1,
            model_context_feedback=(),
            current_step=replace(
                step,
                phase="model_request_completed",
                assistant_message_node_id=assistant_message_node_id,
            ),
        )
        return self._validated(state, next_state)

    def schedule_model_request_retry(self, state: AgentRunState) -> AgentRunState:
        step = self._require_step(
            state,
            phase="model_request_intent_recorded",
        )
        next_state = replace(
            state,
            revision=state.revision + 1,
            current_step=replace(
                step,
                phase="model_request_retry_scheduled",
                retry_count=step.retry_count + 1,
            ),
        )
        return self._validated(state, next_state)

    def prepare_tool_calls(
        self,
        state: AgentRunState,
        *,
        tool_calls: tuple[ToolCallState, ...],
    ) -> AgentRunState:
        step = self._require_step(state, phase="model_request_completed")
        if any(call.execution_state != "ready" for call in tool_calls):
            raise StorageIntegrityError("新 ToolCallState 必须从 ready 开始")
        next_state = replace(
            state,
            revision=state.revision + 1,
            current_step=replace(
                step,
                phase="tool_calls_ready" if tool_calls else "completed",
                tool_calls=tool_calls,
            ),
        )
        return self._validated(state, next_state)

    def record_tool_call_intent(
        self,
        state: AgentRunState,
        *,
        tool_call_id: str,
    ) -> AgentRunState:
        step = self._require_step(
            state,
            phase=("tool_calls_ready", "tool_calls_running"),
        )
        tool_calls = self._replace_tool_call(
            step,
            tool_call_id=tool_call_id,
            expected_execution_state="ready",
            replacement=lambda call: replace(call, execution_state="intent_recorded"),
        )
        next_state = replace(
            state,
            revision=state.revision + 1,
            current_step=replace(
                step,
                phase="tool_calls_running",
                tool_calls=tool_calls,
            ),
        )
        return self._validated(state, next_state)

    def record_tool_call_completed(
        self,
        state: AgentRunState,
        *,
        tool_call_id: str,
        result_message_node_id: str,
        is_error: bool,
        feedback_text: str | None = None,
    ) -> AgentRunState:
        step = self._require_step(state, phase="tool_calls_running")
        tool_calls = self._replace_tool_call(
            step,
            tool_call_id=tool_call_id,
            expected_execution_state="intent_recorded",
            replacement=lambda call: replace(
                call,
                execution_state="completed",
                result_message_node_id=result_message_node_id,
                is_error=is_error,
            ),
        )
        next_state = replace(
            state,
            revision=state.revision + 1,
            current_step=replace(step, tool_calls=tool_calls),
            model_context_feedback=(
                (*state.model_context_feedback, feedback_text)
                if feedback_text
                else state.model_context_feedback
            ),
        )
        return self._validated(state, next_state)

    def record_post_tool_batch_hook_completed(
        self,
        state: AgentRunState,
        *,
        feedback_text: str | None = None,
    ) -> AgentRunState:
        step = self._require_step(
            state,
            phase=("tool_calls_ready", "tool_calls_running"),
        )
        if any(call.execution_state != "completed" for call in step.tool_calls):
            raise StorageIntegrityError(
                "所有 ToolCall 完成后才能记录 PostToolBatch Hook"
            )
        next_state = replace(
            state,
            revision=state.revision + 1,
            current_step=replace(step, post_tool_batch_hook_completed=True),
            model_context_feedback=(
                (*state.model_context_feedback, feedback_text)
                if feedback_text
                else state.model_context_feedback
            ),
        )
        return self._validated(state, next_state)

    def complete_model_step(self, state: AgentRunState) -> AgentRunState:
        step = self._require_step(
            state,
            phase=("tool_calls_ready", "tool_calls_running"),
        )
        if any(call.execution_state != "completed" for call in step.tool_calls):
            raise StorageIntegrityError("存在未完成 ToolCall，不能完成 ModelStep")
        if step.tool_calls and not step.post_tool_batch_hook_completed:
            raise StorageIntegrityError(
                "PostToolBatch Hook 尚未完成，不能完成 ModelStep"
            )
        next_state = replace(
            state,
            revision=state.revision + 1,
            current_step=replace(step, phase="completed"),
        )
        return self._validated(state, next_state)

    def archive_completed_model_step(self, state: AgentRunState) -> AgentRunState:
        step = self._require_step(state, phase="completed")
        next_state = replace(
            state,
            revision=state.revision + 1,
            current_step=None,
            completed_step_ids=(*state.completed_step_ids, step.step_id),
        )
        return self._validated(state, next_state)

    def pause_for_unknown_tool_effect(self, state: AgentRunState) -> AgentRunState:
        step = self._require_step(state, phase="tool_calls_running")
        if not any(
            call.execution_state == "intent_recorded" for call in step.tool_calls
        ):
            raise StorageIntegrityError("只有未知 ToolCall intent 才能暂停 AgentRun")
        next_state = replace(
            state,
            revision=state.revision + 1,
            status="waiting",
        )
        return self._validated(state, next_state)

    def finish_agent_run(
        self,
        state: AgentRunState,
        *,
        final_assistant_node_id: str,
    ) -> AgentRunState:
        step = self._require_step(state, phase="completed")
        next_state = replace(
            state,
            revision=state.revision + 1,
            status="succeeded",
            current_step=None,
            completed_step_ids=(*state.completed_step_ids, step.step_id),
            final_assistant_node_id=final_assistant_node_id,
        )
        return self._validated(state, next_state)

    def create_initial_agent_run_state(
        self,
        *,
        operation_id: str,
        user_message_node_id: str,
        model_context_feedback: tuple[str, ...] = (),
    ) -> AgentRunState:
        return AgentRunState(
            operation_id=operation_id,
            revision=1,
            status="queued",
            user_message_node_id=user_message_node_id,
            model_context_feedback=model_context_feedback,
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

    def _validated(
        self,
        current: AgentRunState,
        next_state: AgentRunState,
    ) -> AgentRunState:
        self.validate_agent_run_transition(current=current, next_state=next_state)
        return next_state

    @staticmethod
    def _require_step(
        state: AgentRunState,
        *,
        phase: str | tuple[str, ...],
    ) -> ModelStepState:
        step = state.current_step
        phases = (phase,) if isinstance(phase, str) else phase
        if step is None or step.phase not in phases:
            actual = step.phase if step is not None else "none"
            raise StorageIntegrityError(
                f"当前 ModelStep.phase 不符合转换要求: {actual}, expected={phases}"
            )
        return step

    @staticmethod
    def _replace_tool_call(
        step: ModelStepState,
        *,
        tool_call_id: str,
        expected_execution_state: str,
        replacement,
    ) -> tuple[ToolCallState, ...]:
        found = False
        tool_calls = []
        for call in step.tool_calls:
            if call.tool_call_id != tool_call_id:
                tool_calls.append(call)
                continue
            found = True
            if call.execution_state != expected_execution_state:
                raise StorageIntegrityError(
                    "ToolCallState 不符合转换要求: "
                    f"{tool_call_id}={call.execution_state}, "
                    f"expected={expected_execution_state}"
                )
            tool_calls.append(replacement(call))
        if not found:
            raise StorageIntegrityError(f"ToolCallState 不存在: {tool_call_id}")
        return tuple(tool_calls)

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
            or next_state.execution_policy != current.execution_policy
            or next_state.decision_reason != current.decision_reason
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
