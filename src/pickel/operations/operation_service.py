"""SessionOperation 接受和 AgentRunState 的窄领域服务。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from uuid import uuid4

from pickel.conversations.conversation_node import ConversationNode
from pickel.inbox.message import InboxMessage
from pickel.operations.agent_run_state import AgentRunState, Cancellation
from pickel.operations.agent_delegation import AgentDelegation
from pickel.operations.operation_store import OperationStore
from pickel.operations.session_operation import SessionOperation
from pickel.shared.storage_errors import StorageIntegrityError
from pickel.operations.agent_run_state_machine import AgentRunStateMachine
from pickel.workspaces.workspace_binding import WorkspaceBinding


class OperationNotFoundError(LookupError):
    """请求的 Operation 不存在。"""


class AgentRunStateNotFoundError(LookupError):
    """请求的 AgentRunState 不存在。"""


@dataclass(frozen=True)
class AcceptedOperation:
    """接受事务提交后的两个稳定实体。"""

    operation: SessionOperation
    state: AgentRunState


class OperationService:
    """只负责接受、读取和 CAS 提交，不调用 Provider、Tool 或 Hook。"""

    def __init__(
        self,
        store: OperationStore,
        *,
        operation_id_factory: Callable[[], str] | None = None,
        now: Callable[[], datetime] | None = None,
        state_machine: AgentRunStateMachine | None = None,
    ) -> None:
        self._store = store
        self._operation_id_factory = operation_id_factory or (lambda: str(uuid4()))
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._state_machine = state_machine or AgentRunStateMachine()

    def accept_pending_message(
        self,
        *,
        message: InboxMessage,
        agent_package_version_id: str,
        workspace_binding: WorkspaceBinding,
        expected_node_id: str | None,
        accepted_at: datetime | None = None,
    ) -> AcceptedOperation | None:
        """把一个已持久化的 pending InboxMessage 原子接受为 Operation。

        `message` 是调用方从 Inbox 读到的窄投影；真正的 pending CAS、Node、
        Operation、State 和 Session 指针由 Store 的唯一原子方法负责。竞争
        失败返回 ``None``，调用方重新读取 Session/InBox 后再决定是否唤醒。
        """
        if message.status != "pending":
            raise StorageIntegrityError("只有 pending InboxMessage 才能接受 Operation")
        if not agent_package_version_id:
            raise ValueError("agent_package_version_id 不能为空")
        if workspace_binding.workspace_id == "":
            raise ValueError("workspace_binding.workspace_id 不能为空")

        timestamp = accepted_at or self._now()
        operation = SessionOperation(
            operation_id=self._operation_id_factory(),
            session_id=message.session_id,
            agent_package_version_id=agent_package_version_id,
            workspace_binding=workspace_binding,
            input_node_id=message.message_id,
            accepted_at=timestamp,
        )
        state = self._state_machine.create_queued(operation.operation_id)
        accepted = self._store.accept_operation(
            operation=operation,
            state=state,
            expected_node_id=expected_node_id,
        )
        if not accepted:
            return None
        return AcceptedOperation(operation=operation, state=state)

    def load_operation(self, operation_id: str) -> SessionOperation:
        operation = self._store.load_operation(operation_id)
        if operation is None:
            raise OperationNotFoundError(f"SessionOperation 不存在: {operation_id}")
        return operation

    def list_operations(self, *, session_id: str) -> tuple[SessionOperation, ...]:
        return self._store.list_operations(session_id=session_id)

    def load_delegation(self, child_session_id: str) -> AgentDelegation | None:
        return self._store.load_delegation(child_session_id)

    def list_delegations(
        self, *, parent_operation_id: str
    ) -> tuple[AgentDelegation, ...]:
        return self._store.list_delegations(parent_operation_id=parent_operation_id)

    def list_pending_step_messages(
        self, *, session_id: str
    ) -> tuple[InboxMessage, ...]:
        return self._store.list_pending_step_messages(session_id=session_id)

    def reconcile_cancellation(
        self, operation_id: str, *, reason: str | None = None
    ) -> tuple[str, ...]:
        """重复推进 parent 的后代取消，并清理已授权的 child Inbox。"""
        root_state = self.load_agent_run_state(operation_id)
        if root_state.cancellation is None:
            raise StorageIntegrityError("cancelling Operation 缺少 Cancellation")
        cancellation_reason = reason or root_state.cancellation.cause
        operation_ids: set[str] = {operation_id}
        wake_sessions: set[str] = set()
        frontier = [operation_id]
        while frontier:
            parent_operation_id = frontier.pop()
            for delegation in self._store.list_delegations(
                parent_operation_id=parent_operation_id
            ):
                for child_operation in self._store.list_operations(
                    session_id=delegation.child_session_id
                ):
                    if child_operation.operation_id in operation_ids:
                        continue
                    operation_ids.add(child_operation.operation_id)
                    frontier.append(child_operation.operation_id)
                    child_state = self._store.load_run_state(
                        child_operation.operation_id
                    )
                    if child_state is None or child_state.status in {
                        "succeeded",
                        "failed",
                        "cancelled",
                    }:
                        continue
                    wake_sessions.add(child_operation.session_id)
                    if child_state.status == "cancelling":
                        continue
                    step = child_state.current_step
                    has_unknown_effect = step is not None and any(
                        call.status == "intent_recorded"
                        and call.replay_policy == "never"
                        for call in step.tool_calls
                    )
                    next_state = replace(
                        child_state,
                        revision=child_state.revision + 1,
                        status="cancelling",
                        waiting_reason=None,
                        current_step=step if has_unknown_effect else None,
                        cancellation=Cancellation(
                            cause=cancellation_reason,
                            requested_at=self._now(),
                        ),
                    )
                    if self.commit_transition(
                        state=next_state,
                        expected_revision=child_state.revision,
                        node=None,
                        updated_at=next_state.cancellation.requested_at,
                    ):
                        wake_sessions.add(child_operation.session_id)

        self._store.discard_cancellation_messages(
            root_operation_id=operation_id,
            reason="祖先 Operation 已取消",
            handled_at=self._now(),
        )
        return tuple(sorted(wake_sessions))

    def cancellation_ready(self, operation_id: str) -> bool:
        return self._store.cancellation_ready(root_operation_id=operation_id)

    def parent_session_id(self, operation_id: str) -> str | None:
        operation = self.load_operation(operation_id)
        delegation = self._store.load_delegation(operation.session_id)
        if delegation is None:
            return None
        parent = self._store.load_operation(delegation.parent_operation_id)
        return parent.session_id if parent is not None else None

    def claim_step_messages(
        self,
        *,
        message_ids: tuple[str, ...],
        state: AgentRunState,
        expected_revision: int,
        updated_at: datetime,
    ) -> bool:
        current = self.load_agent_run_state(state.operation_id)
        if current.revision != expected_revision:
            return False
        self._state_machine.validate_transition(current=current, next_state=state)
        return self._store.claim_step_messages(
            message_ids=message_ids,
            state=state,
            expected_revision=expected_revision,
            updated_at=updated_at,
        )

    def load_agent_run_state(self, operation_id: str) -> AgentRunState:
        self.load_operation(operation_id)
        state = self._store.load_run_state(operation_id)
        if state is None:
            raise AgentRunStateNotFoundError(f"AgentRunState 不存在: {operation_id}")
        if state.operation_id != operation_id:
            raise StorageIntegrityError("AgentRunState.operation_id 与查询身份不匹配")
        return state

    def commit_state(
        self,
        *,
        state: AgentRunState,
        expected_revision: int,
        updated_at: datetime | None = None,
    ) -> bool:
        """校验状态转换后执行一次显式 revision CAS。

        Store 返回 ``False`` 表示竞争失败；服务不重读、不盲目重放，也不
        执行任何外部副作用。
        """
        return self.commit_transition(
            state=state,
            expected_revision=expected_revision,
            node=None,
            updated_at=updated_at,
        )

    def commit_transition(
        self,
        *,
        state: AgentRunState,
        expected_revision: int,
        node: ConversationNode | None,
        updated_at: datetime | None = None,
    ) -> bool:
        """校验并原子提交状态与它首次引用的 ConversationNode。"""
        current = self.load_agent_run_state(state.operation_id)
        if current.revision != expected_revision:
            return False
        self._state_machine.validate_transition(current=current, next_state=state)
        return self._store.commit_run_transition(
            state=state,
            expected_revision=expected_revision,
            node=node,
            updated_at=updated_at or self._now(),
        )

    def request_cancellation(
        self,
        operation_id: str,
        *,
        reason: str,
        requested_at: datetime | None = None,
    ) -> bool:
        """一次 CAS 请求取消；竞争失败由调用方重新唤醒后重读。"""
        if not reason:
            raise ValueError("取消原因不能为空")
        current = self.load_agent_run_state(operation_id)
        if current.status == "cancelled":
            return True
        if current.status in {"succeeded", "failed"}:
            return False
        if current.status == "cancelling":
            accepted = (
                current.cancellation is not None
                and current.cancellation.cause == reason
            )
            if accepted:
                self.reconcile_cancellation(operation_id, reason=reason)
            return accepted

        step = current.current_step
        has_unknown_effect = step is not None and any(
            call.status == "intent_recorded" and call.replay_policy == "never"
            for call in step.tool_calls
        )
        timestamp = requested_at or self._now()
        next_state = replace(
            current,
            revision=current.revision + 1,
            status="cancelling",
            waiting_reason=None,
            current_step=step if has_unknown_effect else None,
            cancellation=Cancellation(cause=reason, requested_at=timestamp),
        )
        accepted = self.commit_transition(
            state=next_state,
            expected_revision=current.revision,
            node=None,
            updated_at=timestamp,
        )
        if not accepted:
            return False
        self.reconcile_cancellation(operation_id, reason=reason)
        return True
