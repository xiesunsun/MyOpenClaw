"""AgentRunState 的纯状态转换校验。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pickel.operations.agent_run_state import AgentRunState
from pickel.persistence.errors import StorageIntegrityError

RunAction = Literal[
    "start",
    "record_request_intent",
    "execute_request",
    "execute_tools",
    "wait",
    "done",
]


@dataclass(frozen=True)
class AgentRunDecision:
    """状态机给 Driver 的下一动作提示；不包含可执行对象。"""

    action: RunAction


class AgentRunStateMachine:
    """校验可恢复状态转换，不读写 Store，不执行 Provider/Tool。"""

    _RUN_TRANSITIONS = {
        "queued": {"running", "cancelling", "failed"},
        "running": {"running", "waiting", "cancelling", "succeeded", "failed"},
        # 一个 Step 可以包含多个审批；逐个决定时仍保持 waiting。
        "waiting": {"waiting", "running", "cancelling", "failed"},
        "cancelling": {"cancelled"},
        "succeeded": set(),
        "failed": set(),
        "cancelled": set(),
    }
    _STEP_TRANSITIONS = {
        "preparing_request": {"preparing_request", "request_ready"},
        "request_ready": {"request_ready", "awaiting_tools"},
        "awaiting_tools": {"awaiting_tools"},
    }

    def create_queued(self, operation_id: str) -> AgentRunState:
        """创建接受事务使用的唯一初始状态。"""
        return AgentRunState(
            operation_id=operation_id,
            revision=1,
            status="queued",
            waiting_reason=None,
            completed_step_count=0,
            current_step=None,
            final_assistant_node_id=None,
            error=None,
            cancellation=None,
        )

    def decide_next(self, state: AgentRunState) -> AgentRunDecision:
        """仅从持久化状态决定 Driver 的窄动作。"""
        if state.status in {"succeeded", "failed", "cancelled"}:
            return AgentRunDecision("done")
        if state.status == "waiting":
            return AgentRunDecision("wait")
        if state.current_step is None:
            return AgentRunDecision("start")
        if state.current_step.phase == "preparing_request":
            return AgentRunDecision("record_request_intent")
        if state.current_step.phase == "request_ready":
            return AgentRunDecision("execute_request")
        return AgentRunDecision("execute_tools")

    def validate_transition(
        self,
        *,
        current: AgentRunState,
        next_state: AgentRunState,
    ) -> None:
        """验证一次完整状态写入，要求 revision 严格递增一位。"""
        if current.operation_id != next_state.operation_id:
            raise StorageIntegrityError("AgentRunState.operation_id 不能改变")
        if next_state.revision != current.revision + 1:
            raise StorageIntegrityError(
                "AgentRunState.revision 必须连续递增: "
                f"expected={current.revision + 1}, actual={next_state.revision}"
            )
        if next_state.status not in self._RUN_TRANSITIONS[current.status]:
            raise StorageIntegrityError(
                "非法 AgentRunState.status 转换: "
                f"{current.status} -> {next_state.status}"
            )
        self._validate_waiting(current)
        self._validate_waiting(next_state)
        self._validate_approval_state(current)
        self._validate_approval_state(next_state)
        self._validate_step(current=current, next_state=next_state)
        self._validate_completion(current=current, next_state=next_state)

    @staticmethod
    def _validate_waiting(state: AgentRunState) -> None:
        if state.status != "waiting":
            return
        step = state.current_step
        if step is None or step.phase != "awaiting_tools":
            raise StorageIntegrityError(
                "waiting AgentRunState 必须保留 awaiting_tools current_step"
            )
        statuses = {call.status for call in step.tool_calls}
        if state.waiting_reason == "tool_approval":
            if "waiting_approval" not in statuses:
                raise StorageIntegrityError(
                    "tool_approval 必须有 waiting_approval ToolCall"
                )
        elif state.waiting_reason == "tool_reconciliation":
            if "intent_recorded" not in statuses:
                raise StorageIntegrityError(
                    "tool_reconciliation 必须有 intent_recorded ToolCall"
                )

    @staticmethod
    def _validate_approval_state(state: AgentRunState) -> None:
        step = state.current_step
        if step is None:
            return
        if any(call.status == "waiting_approval" for call in step.tool_calls):
            if state.status != "waiting" or state.waiting_reason != "tool_approval":
                raise StorageIntegrityError(
                    "存在 waiting_approval ToolCall 时 AgentRunState 必须等待审批"
                )

    @classmethod
    def _validate_step(
        cls,
        *,
        current: AgentRunState,
        next_state: AgentRunState,
    ) -> None:
        current_step = current.current_step
        next_step = next_state.current_step

        if next_state.status in {"succeeded", "failed", "cancelled"}:
            if next_step is not None:
                raise StorageIntegrityError("终态 AgentRunState 必须清空 current_step")
            return
        if current_step is None:
            if next_step is None:
                return
            if next_step.step_sequence != current.completed_step_count + 1:
                raise StorageIntegrityError("ModelStep.step_sequence 不连续")
            if next_step.phase != "preparing_request":
                raise StorageIntegrityError(
                    "新 ModelStep 必须从 preparing_request 开始"
                )
            return
        if next_step is None:
            if next_state.status not in {"running", "cancelling"}:
                raise StorageIntegrityError("非终态不能清空未完成 current_step")
            if next_state.status == "cancelling":
                return
            if current_step.phase != "awaiting_tools":
                raise StorageIntegrityError(
                    "只有完成 awaiting_tools 才能清空当前 ModelStep"
                )
            if any(call.status != "completed" for call in current_step.tool_calls):
                raise StorageIntegrityError("存在未完成 ToolCall，不能完成 ModelStep")
            return
        if next_step.step_id != current_step.step_id:
            raise StorageIntegrityError("当前 ModelStep 的 step_id 不能改变")
        if next_step.step_sequence != current_step.step_sequence:
            raise StorageIntegrityError("当前 ModelStep 的 step_sequence 不能改变")
        allowed = cls._STEP_TRANSITIONS[current_step.phase]
        if next_step.phase not in allowed:
            raise StorageIntegrityError(
                "非法 ModelStep.phase 转换: "
                f"{current_step.phase} -> {next_step.phase}"
            )
        cls._validate_model_request(current_step, next_step)
        if current_step.phase == "awaiting_tools":
            cls._validate_tool_calls(current_step, next_step)

    @staticmethod
    def _validate_model_request(current_step, next_step) -> None:
        if current_step.phase == "preparing_request":
            if next_step.phase == "preparing_request" and next_step != current_step:
                raise StorageIntegrityError("preparing_request 阶段的请求事实不能改写")
            if next_step.phase == "request_ready" and (
                next_step.request_attempt != current_step.request_attempt
            ):
                raise StorageIntegrityError(
                    "首次写入 ModelRequestIntent 时 request_attempt 不能改变"
                )
            return
        if current_step.phase == "request_ready":
            if next_step.request_attempt not in {
                current_step.request_attempt,
                current_step.request_attempt + 1,
            }:
                raise StorageIntegrityError(
                    "request_ready 自循环只能保持或递增一次 request_attempt"
                )
            if next_step.phase == "request_ready" and (
                next_step.request_intent != current_step.request_intent
            ):
                raise StorageIntegrityError("ModelRequestIntent 创建后不能被改写")
            return
        if next_step.request_attempt != current_step.request_attempt:
            raise StorageIntegrityError("ModelStep.request_attempt 不能改变")
        if (
            next_step.assistant_message_node_id
            != current_step.assistant_message_node_id
        ):
            raise StorageIntegrityError("assistant_message_node_id 不能被改写")

    @staticmethod
    def _validate_tool_calls(current_step, next_step) -> None:
        if len(current_step.tool_calls) != len(next_step.tool_calls):
            raise StorageIntegrityError("ToolCallState 列表长度不能改变")
        for current, next_call in zip(
            current_step.tool_calls, next_step.tool_calls, strict=True
        ):
            if (
                current.tool_call_id != next_call.tool_call_id
                or current.tool_name != next_call.tool_name
                or current.arguments != next_call.arguments
                or current.replay_policy != next_call.replay_policy
            ):
                raise StorageIntegrityError("ToolCallState 身份和参数不能改变")
            allowed = {
                "waiting_approval": {"waiting_approval", "ready", "denied"},
                "ready": {"ready", "intent_recorded"},
                "denied": {"denied", "completed"},
                "intent_recorded": {"intent_recorded", "completed"},
                "completed": {"completed"},
            }[current.status]
            if next_call.status not in allowed:
                raise StorageIntegrityError(
                    "非法 ToolCallState.status 转换: "
                    f"{current.status} -> {next_call.status}"
                )
            if current.execution_intent != next_call.execution_intent and not (
                current.status == "ready" and next_call.status == "intent_recorded"
            ):
                raise StorageIntegrityError("ToolExecutionIntent 只能在执行前写入")
            if current.approval is not None:
                if next_call.approval is None:
                    raise StorageIntegrityError("ToolApproval 不能被清除")
                if (
                    current.approval.requested_at != next_call.approval.requested_at
                    or current.approval.requested_by != next_call.approval.requested_by
                    or current.approval.reason != next_call.approval.reason
                ):
                    raise StorageIntegrityError("ToolApproval 请求内容不能被改写")
                if (
                    current.approval.decision is not None
                    and next_call.approval.decision != current.approval.decision
                ):
                    raise StorageIntegrityError("ToolApproval 决定不能被改写")
                if (
                    current.status != "waiting_approval"
                    and next_call.approval != current.approval
                ):
                    raise StorageIntegrityError("ToolApproval 决定后不能被改写")
                if current.status == "waiting_approval":
                    decision = next_call.approval.decision
                    if next_call.status == "ready":
                        if decision is None or decision.outcome != "approved":
                            raise StorageIntegrityError(
                                "ready ToolCall 必须有 approved 决定"
                            )
                    elif next_call.status == "denied":
                        if decision is None or decision.outcome != "denied":
                            raise StorageIntegrityError(
                                "denied ToolCall 必须有 denied 决定"
                            )
                    elif decision is not None:
                        raise StorageIntegrityError("waiting_approval 不能提前写入决定")
            elif next_call.approval is not None:
                raise StorageIntegrityError("ToolApproval 只能在初始 ToolCall 写入")
            if current.status == "completed":
                if (
                    next_call.result_node_id != current.result_node_id
                    or next_call.is_error != current.is_error
                ):
                    raise StorageIntegrityError("ToolResult 不能被改写或清除")
            if current.status in {"intent_recorded", "denied"} and (
                next_call.status == current.status
                and next_call.result_node_id != current.result_node_id
            ):
                raise StorageIntegrityError("ToolResult 只能在完成时首次写入")

    @staticmethod
    def _validate_completion(
        *,
        current: AgentRunState,
        next_state: AgentRunState,
    ) -> None:
        if next_state.completed_step_count < current.completed_step_count:
            raise StorageIntegrityError("completed_step_count 不能减少")
        if next_state.completed_step_count > current.completed_step_count + 1:
            raise StorageIntegrityError("一次状态转换最多完成一个 ModelStep")
        if next_state.current_step is None and current.current_step is not None:
            if next_state.status == "cancelling":
                expected_count = current.completed_step_count
            elif next_state.status == "running":
                if current.current_step.phase != "awaiting_tools" or any(
                    call.status != "completed"
                    for call in current.current_step.tool_calls
                ):
                    raise StorageIntegrityError("只有完成当前 ModelStep 才能继续运行")
                expected_count = current.completed_step_count + 1
            elif next_state.status in {"succeeded", "failed", "cancelled"}:
                expected_count = (
                    current.completed_step_count + 1
                    if next_state.status == "succeeded"
                    else current.completed_step_count
                )
            else:
                raise StorageIntegrityError("清空 ModelStep 必须进入运行态或终态")
            if next_state.completed_step_count != expected_count:
                raise StorageIntegrityError(
                    "清空 ModelStep 时 completed_step_count 不正确"
                )
