"""Conversation 领域依赖的持久化窄接口。"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from pickel.conversations.conversation_node import ConversationEntry
from pickel.conversations.conversation_session import ConversationSession
from pickel.persistence.immutable_object import ImmutableObject
from pickel.persistence.named_reference import NamedReference
from pickel.persistence.storage_transaction import StorageTransaction


class ConversationStore(Protocol):
    def create_conversation_session(
        self,
        *,
        session_id: str,
        agent_id: str,
        cwd: str,
        created_at: datetime | None = None,
    ) -> None: ...

    def load_conversation_session(
        self,
        session_id: str,
    ) -> ConversationSession | None: ...

    def list_conversation_sessions(
        self,
        *,
        limit: int = 20,
        cwd: str | None = None,
    ) -> list[ConversationSession]: ...

    def archive_conversation_session(
        self,
        *,
        session_id: str,
        archived_at: datetime,
    ) -> None: ...

    def delete_conversation_session(self, *, session_id: str) -> None: ...

    def begin_storage_transaction(
        self,
        *,
        session_id: str,
        expected_sequence: int,
    ) -> StorageTransaction: ...

    def load_immutable_object(self, object_id: str) -> ImmutableObject | None: ...

    def find_named_reference(
        self,
        *,
        session_id: str,
        reference_name: str,
    ) -> NamedReference | None: ...

    def list_active_branch_entries(
        self,
        *,
        session_id: str,
        reference_name: str = "conversation/active",
    ) -> list[ConversationEntry]: ...
