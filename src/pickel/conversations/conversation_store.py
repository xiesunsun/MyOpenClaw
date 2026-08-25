"""Conversation 领域的 v10 持久化窄接口。"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from pickel.conversations.conversation_node import ConversationNode
from pickel.conversations.conversation_session import ConversationSession
from pickel.workspaces.workspace import Workspace


class ConversationStore(Protocol):
    def create_session(
        self, *, workspace: Workspace, session: ConversationSession
    ) -> None: ...

    def load_session(self, session_id: str) -> ConversationSession | None: ...

    def list_sessions(
        self, *, limit: int = 20, cwd: str | None = None
    ) -> tuple[ConversationSession, ...]: ...

    def list_runnable_session_ids(self) -> tuple[str, ...]: ...

    def append_node(
        self,
        *,
        node: ConversationNode,
        expected_node_id: str | None,
    ) -> bool: ...

    def load_node(self, node_id: str) -> ConversationNode | None: ...

    def list_branch_nodes(
        self, session_id: str, leaf_node_id: str | None
    ) -> tuple[ConversationNode, ...]: ...

    def move_active_node(
        self,
        *,
        session_id: str,
        expected_node_id: str | None,
        new_node_id: str | None,
        updated_at: datetime,
    ) -> bool: ...

    def archive_session(self, *, session_id: str, archived_at: datetime) -> None: ...

    def unarchive_session(self, *, session_id: str, updated_at: datetime) -> None: ...

    def delete_session(self, *, session_id: str) -> None: ...

    def delete_session_tree(self, *, session_id: str) -> None: ...
