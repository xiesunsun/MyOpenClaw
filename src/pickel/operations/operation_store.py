"""Operation 领域需要的最小持久化端口。"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from pickel.conversations.conversation_node import ConversationNode
from pickel.inbox.message import InboxMessage
from pickel.operations.agent_run_state import AgentRunState
from pickel.operations.agent_delegation import AgentDelegation
from pickel.operations.session_operation import SessionOperation


class OperationStore(Protocol):
    """OperationService 的窄依赖。

    `accept_operation` 是唯一的创建入口：Inbox claim、输入 Node、Operation、
    初始 State 和 Session active 指针必须由具体 Store 在一个事务内完成。
    """

    def accept_operation(
        self,
        *,
        operation: SessionOperation,
        state: AgentRunState,
        expected_node_id: str | None,
    ) -> bool: ...

    def load_operation(self, operation_id: str) -> SessionOperation | None: ...

    def list_operations(self, *, session_id: str) -> tuple[SessionOperation, ...]: ...

    def load_delegation(self, child_session_id: str) -> AgentDelegation | None: ...

    def list_delegations(
        self, *, parent_operation_id: str
    ) -> tuple[AgentDelegation, ...]: ...

    def discard_cancellation_messages(
        self, *, root_operation_id: str, reason: str, handled_at: datetime
    ) -> tuple[str, ...]: ...

    def cancellation_ready(self, *, root_operation_id: str) -> bool: ...

    def list_pending_step_messages(
        self, *, session_id: str
    ) -> tuple[InboxMessage, ...]: ...

    def claim_step_messages(
        self,
        *,
        message_ids: tuple[str, ...],
        state: AgentRunState,
        expected_revision: int,
        updated_at: datetime,
    ) -> bool: ...

    def load_run_state(self, operation_id: str) -> AgentRunState | None: ...

    def commit_run_transition(
        self,
        *,
        state: AgentRunState,
        expected_revision: int,
        node: ConversationNode | None,
        updated_at: datetime,
    ) -> bool:
        """唯一 State CAS、可选 ConversationNode 与 Session 指针原子入口。"""
        ...
