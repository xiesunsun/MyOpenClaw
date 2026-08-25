"""Operation 领域需要的最小持久化端口。"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from pickel.conversations.conversation_node import ConversationNode
from pickel.operations.agent_run_state import AgentRunState
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

    def load_run_state(self, operation_id: str) -> AgentRunState | None: ...

    def commit_run_transition(
        self,
        *,
        state: AgentRunState,
        expected_revision: int,
        node: ConversationNode | None,
        updated_at: datetime,
    ) -> bool:
        """原子提交可选 ConversationNode 与 AgentRunState CAS。"""
        ...
