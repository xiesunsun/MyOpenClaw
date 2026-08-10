"""Operation 领域依赖的持久化窄接口。"""

from __future__ import annotations

from typing import Protocol

from pickel.agents.agent_package_store import AgentPackageVersionStore
from pickel.conversations.conversation_store import ConversationStore
from pickel.operations.session_operation import SessionOperation


class OperationStore(ConversationStore, AgentPackageVersionStore, Protocol):
    def load_session_operation(
        self,
        operation_id: str,
    ) -> SessionOperation | None: ...

    def list_session_operations(
        self,
        *,
        session_id: str,
    ) -> list[SessionOperation]: ...
