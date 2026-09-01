"""ToolApproval 的查询和 revision CAS 决定服务。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Literal, Mapping

from pickel.operations.agent_run_state import (
    AgentRunState,
    ToolApprovalDecision,
    ToolCallState,
)
from pickel.operations.operation_service import OperationService
from pickel.shared.storage_errors import StorageConflictError

ApprovalOutcome = Literal["approved", "denied"]


@dataclass(frozen=True)
class PendingToolApproval:
    """从 AgentRunState 投影出的待审批查询项，不单独落库。"""

    operation_id: str
    session_id: str
    revision: int
    step_id: str
    tool_call_id: str
    tool_name: str
    arguments: Mapping[str, object]
    requested_at: datetime
    requested_by: str
    request_reason: str | None


class ApprovalService:
    """通过 AgentRunState 的一次 revision CAS 接受 ToolApproval 决定。

    Approval 不拥有独立表或队列；查询和写入都围绕当前 Operation 的
    ``AgentRunState.current_step.tool_calls`` 进行。CAS 失败不会在服务内
    重读或重试，调用方应重新查询后再决定下一步。
    """

    def __init__(
        self,
        operation_service: OperationService,
        *,
        wake: Callable[[str], None],
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._operations = operation_service
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._wake = wake

    def list_pending_approvals(
        self, operation_id: str
    ) -> tuple[PendingToolApproval, ...]:
        """按 Provider 原始顺序返回当前 Operation 的待审批查询投影。"""
        operation = self._operations.load_operation(operation_id)
        state = self._operations.load_agent_run_state(operation_id)
        step = state.current_step
        if step is None:
            return ()
        return tuple(
            PendingToolApproval(
                operation_id=operation_id,
                session_id=operation.session_id,
                revision=state.revision,
                step_id=step.step_id,
                tool_call_id=call.tool_call_id,
                tool_name=call.tool_name,
                arguments=call.arguments,
                requested_at=call.approval.requested_at,
                requested_by=call.approval.requested_by,
                request_reason=call.approval.reason,
            )
            for call in step.tool_calls
            if call.status == "waiting_approval"
        )

    def submit_tool_approval(
        self,
        operation_id: str,
        step_id: str,
        tool_call_id: str,
        *,
        outcome: ApprovalOutcome,
        expected_revision: int,
        actor_id: str | None = None,
        reason: str | None = None,
        decided_at: datetime | None = None,
    ) -> AgentRunState:
        """接受一次批准或拒绝。

        未决决定必须匹配 ``expected_revision``，并只尝试一次 CAS。重复提交
        同一个 ``outcome + actor_id + reason`` 时直接返回当前状态；它不因
        第一次提交已经推进 revision 而失败。不同决定、取消中/已取消和终态
        Operation 统一视为不可接受的 StorageConflictError。
        """
        if outcome not in ("approved", "denied"):
            raise ValueError(f"不支持的 approval outcome: {outcome!r}")
        if not tool_call_id:
            raise ValueError("tool_call_id 不能为空")
        if not step_id:
            raise ValueError("step_id 不能为空")
        if actor_id == "":
            raise ValueError("actor_id 不能为空字符串")

        current = self._operations.load_agent_run_state(operation_id)
        if current.status in {
            "cancelling",
            "cancelled",
            "succeeded",
            "failed",
        }:
            raise StorageConflictError(
                f"Operation 不再接受 Approval: {operation_id} ({current.status})"
            )
        if current.current_step is None or current.current_step.step_id != step_id:
            raise StorageConflictError(f"Approval step_id 已过期: expected={step_id}")

        call_index, current_call = self._find_call(current, tool_call_id)
        approval = current_call.approval
        if approval is None:
            raise LookupError(f"ToolCall 没有 Approval: {tool_call_id}")

        existing = approval.decision
        if existing is not None:
            if self._same_decision(
                existing,
                outcome=outcome,
                actor_id=actor_id,
                reason=reason,
            ):
                return current
            raise StorageConflictError(
                f"ToolCall 已有冲突的 Approval 决定: {tool_call_id}"
            )

        if current_call.status != "waiting_approval":
            raise StorageConflictError(
                f"ToolCall 不再等待 Approval: {tool_call_id} ({current_call.status})"
            )
        if current.revision != expected_revision:
            raise StorageConflictError(
                "Approval expected_revision 已过期: "
                f"expected={expected_revision}, actual={current.revision}"
            )

        decision = ToolApprovalDecision(
            outcome=outcome,
            decided_at=decided_at or self._now(),
            actor_id=actor_id,
            reason=reason,
        )
        next_call = replace(
            current_call,
            status="ready" if outcome == "approved" else "rejected",
            approval=replace(approval, decision=decision),
        )
        assert current.current_step is not None
        calls = list(current.current_step.tool_calls)
        calls[call_index] = next_call
        next_step = replace(current.current_step, tool_calls=tuple(calls))
        has_pending = any(call.status == "waiting_approval" for call in calls)
        next_state = replace(
            current,
            revision=current.revision + 1,
            status="waiting" if has_pending else "running",
            waiting_reason="tool_approval" if has_pending else None,
            current_step=next_step,
        )
        if not self._operations.commit_transition(
            state=next_state,
            expected_revision=expected_revision,
            node=None,
            updated_at=decision.decided_at,
        ):
            raise StorageConflictError(
                f"Approval CAS 失败，Operation 已被其他动作修改: {operation_id}"
            )
        if not has_pending:
            operation = self._operations.load_operation(operation_id)
            self._wake(operation.session_id)
        return next_state

    @staticmethod
    def _find_call(
        state: AgentRunState, tool_call_id: str
    ) -> tuple[int, ToolCallState]:
        step = state.current_step
        if step is None:
            raise LookupError(f"ToolCall 不存在: {tool_call_id}")
        for index, call in enumerate(step.tool_calls):
            if call.tool_call_id == tool_call_id:
                return index, call
        raise LookupError(f"ToolCall 不存在: {tool_call_id}")

    @staticmethod
    def _same_decision(
        existing: ToolApprovalDecision,
        *,
        outcome: ApprovalOutcome,
        actor_id: str | None,
        reason: str | None,
    ) -> bool:
        return (
            existing.outcome == outcome
            and existing.actor_id == actor_id
            and existing.reason == reason
        )
