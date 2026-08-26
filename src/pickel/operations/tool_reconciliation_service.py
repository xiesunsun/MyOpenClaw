"""Host 对已提交 ToolExecutionIntent 的恢复核对入口。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timezone
from typing import Literal
from uuid import uuid4

from pickel.conversations.agent_message import ToolResultMessage
from pickel.conversations.content_blocks import TextBlock
from pickel.conversations.conversation_node import ConversationNode
from pickel.conversations.conversation_service import ConversationService
from pickel.operations.agent_run_state import AgentRunState, ToolCallState
from pickel.operations.operation_service import OperationService
from pickel.persistence.errors import StorageConflictError
from pickel.tools.base import ToolExecutionResult
from pickel.tools.validation import invalid_tool_result, validate_tool_result

ToolReconciliationOutcome = Literal["completed", "not_started", "unknown"]


class ToolReconciliationService:
    """通过一次 revision CAS 接受 Host 对 Tool Intent 的核对结果。

    ``intent_recorded`` 不能从状态本身推断工具是否已经产生副作用。该服务
    只接受当前 ModelStep 中 Provider 顺序的第一个未完成调用，且不在 CAS
    失败后重读或重试。``completed`` 的结果节点和 ToolCall 完成状态使用
    OperationStore 的同一事务提交。
    """

    def __init__(
        self,
        operation_service: OperationService,
        conversation_service: ConversationService,
        *,
        wake: Callable[[str], None],
        node_id_factory: Callable[[], str] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._operations = operation_service
        self._conversations = conversation_service
        self._wake = wake
        self._node_id = node_id_factory or (lambda: str(uuid4()))
        self._now = now or (lambda: datetime.now(timezone.utc))

    def reconcile_tool_call(
        self,
        operation_id: str,
        step_id: str,
        tool_call_id: str,
        *,
        outcome: ToolReconciliationOutcome,
        expected_revision: int,
        result: ToolExecutionResult | None = None,
        reconciled_at: datetime | None = None,
    ) -> AgentRunState:
        """接受一个 intent 的 Host 核对结果并返回最新 AgentRunState。

        过期 revision、错误步骤/调用位置、非 reconciliation waiting 和终态
        都是 ``StorageConflictError``。``unknown`` 以及 ``waiting + never``
        的 ``not_started`` 不写入状态，也不会唤醒 Session。
        """
        if outcome not in {"completed", "not_started", "unknown"}:
            raise ValueError(f"不支持的 Tool reconciliation outcome: {outcome!r}")
        if not operation_id or not step_id or not tool_call_id:
            raise ValueError("operation_id、step_id 和 tool_call_id 不能为空")
        if outcome == "completed" and result is None:
            raise ValueError("completed reconciliation 必须提供 result")
        if outcome != "completed" and result is not None:
            raise ValueError(f"{outcome} reconciliation 不能提供 result")

        current = self._operations.load_agent_run_state(operation_id)
        if current.status in {"succeeded", "failed", "cancelled"}:
            raise StorageConflictError(
                f"Operation 不再接受 Tool reconciliation: {operation_id} ({current.status})"
            )
        if current.status == "waiting":
            if current.waiting_reason != "tool_reconciliation":
                raise StorageConflictError(
                    "Tool reconciliation 只接受 waiting/tool_reconciliation"
                )
        elif current.status != "cancelling":
            raise StorageConflictError(
                f"Operation 不在可核对状态: {operation_id} ({current.status})"
            )

        if current.revision != expected_revision:
            raise StorageConflictError(
                "Tool reconciliation expected_revision 已过期: "
                f"expected={expected_revision}, actual={current.revision}"
            )
        step = current.current_step
        if step is None or step.step_id != step_id:
            raise StorageConflictError(f"Tool reconciliation step_id 已过期: {step_id}")
        call_index, call = self._find_first_uncompleted(step.tool_calls, tool_call_id)
        if call.status != "intent_recorded":
            raise StorageConflictError(
                "Tool reconciliation 目标必须是首个未完成的 intent_recorded ToolCall"
            )

        if outcome == "unknown":
            return current

        timestamp = reconciled_at or self._now()
        operation = self._operations.load_operation(operation_id)

        if outcome == "not_started":
            if current.status == "waiting" and call.replay_policy == "never":
                return current
            if current.status == "cancelling":
                next_state = replace(
                    current,
                    revision=current.revision + 1,
                    status="cancelled",
                    waiting_reason=None,
                    current_step=None,
                )
                committed = self._operations.commit_transition(
                    state=next_state,
                    expected_revision=expected_revision,
                    node=None,
                    updated_at=timestamp,
                )
                if not committed:
                    refreshed = self._operations.load_agent_run_state(operation_id)
                    if refreshed.revision == expected_revision:
                        # Store 门槛未满足时保留 cancelling，等待后代收敛。
                        self._wake(operation.session_id)
                        return refreshed
                    raise StorageConflictError(
                        "Tool reconciliation CAS 失败，Operation 已被其他动作修改"
                    )
                self._wake(operation.session_id)
                return next_state

            next_state = replace(
                current,
                revision=current.revision + 1,
                status="running",
                waiting_reason=None,
            )
            committed = self._commit(
                next_state,
                expected_revision=expected_revision,
                updated_at=timestamp,
            )
            self._wake(operation.session_id)
            return committed

        assert result is not None
        # Host 回报绕过正常 execute 适配器，也必须先保证结构化值可安全
        # 冻结/持久化；Package-specific schema 由执行边界校验。
        validation_error = validate_tool_result(None, result)
        if validation_error is not None:
            result = invalid_tool_result(result, validation_error)
        result_node_id = self._node_id()
        completed_call = replace(
            call,
            status="completed",
            result_node_id=result_node_id,
            is_error=result.is_error,
        )
        calls = list(step.tool_calls)
        calls[call_index] = completed_call

        next_step = replace(step, tool_calls=tuple(calls))
        next_state = replace(
            current,
            revision=current.revision + 1,
            # cancelling 先保留 Step 对新 ToolResult 的引用；Driver 被唤醒后再用
            # 下一次 CAS 进入 cancelled。不能为了单次完成而放宽 Store 引用约束。
            status="cancelling" if current.status == "cancelling" else "running",
            waiting_reason=None,
            current_step=next_step,
        )
        session = self._conversations.load_conversation_session(operation.session_id)
        message = self._tool_result_message(completed_call, result)
        node = ConversationNode(
            node_id=result_node_id,
            session_id=operation.session_id,
            parent_node_id=session.active_node_id,
            content_type="agent_message",
            content=message,
            created_at=timestamp,
        )
        committed = self._commit(
            next_state,
            expected_revision=expected_revision,
            node=node,
            updated_at=timestamp,
        )
        self._wake(operation.session_id)
        return committed

    def _commit(
        self,
        state: AgentRunState,
        *,
        expected_revision: int,
        node: ConversationNode | None = None,
        updated_at: datetime,
    ) -> AgentRunState:
        if not self._operations.commit_transition(
            state=state,
            expected_revision=expected_revision,
            node=node,
            updated_at=updated_at,
        ):
            raise StorageConflictError(
                "Tool reconciliation CAS 失败，Operation 已被其他动作修改"
            )
        return state

    @staticmethod
    def _find_first_uncompleted(
        calls: tuple[ToolCallState, ...], tool_call_id: str
    ) -> tuple[int, ToolCallState]:
        for index, call in enumerate(calls):
            if call.status != "completed":
                if call.tool_call_id != tool_call_id:
                    raise StorageConflictError(
                        "Tool reconciliation 必须按 Provider ToolCall 顺序处理"
                    )
                return index, call
        raise StorageConflictError("当前 ModelStep 没有可核对的未完成 ToolCall")

    @staticmethod
    def _tool_result_message(
        call: ToolCallState, result: ToolExecutionResult
    ) -> ToolResultMessage:
        content = tuple(result.content_blocks) or (TextBlock(text=result.content),)
        return ToolResultMessage(
            tool_call_id=call.tool_call_id,
            tool_name=call.tool_name,
            content=content,
            is_error=result.is_error,
            structured_content=result.structured_content,
        )


__all__ = ["ToolReconciliationOutcome", "ToolReconciliationService"]
