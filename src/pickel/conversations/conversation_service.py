"""基于 StorageTransaction 的会话领域写入服务。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from pickel.conversations.agent_message import (
    AssistantMessage,
    ToolResultMessage,
    UserMessage,
    agent_message_to_dict,
)
from pickel.conversations.conversation_node import ConversationEntry
from pickel.conversations.conversation_session import ConversationSession
from pickel.conversations.conversation_store import ConversationStore
from pickel.conversations.session_preview import (
    SessionPreview,
    build_conversation_preview,
)

ACTIVE_CONVERSATION_REFERENCE = "conversation/active"


class ConversationNotFoundError(LookupError):
    pass


class ConversationService:
    """创建会话，并通过唯一原子写路径追加或移动会话树。"""

    def __init__(
        self,
        store: ConversationStore,
        *,
        session_id_factory: Callable[[], str] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._session_id_factory = session_id_factory or (lambda: str(uuid4()))
        self._now = now or (lambda: datetime.now(timezone.utc))

    def create_conversation_session(
        self,
        *,
        agent_id: str,
        cwd: str | None = None,
    ) -> ConversationSession:
        session_id = self._session_id_factory()
        self._store.create_conversation_session(
            session_id=session_id,
            agent_id=agent_id,
            cwd=self._normalize_cwd(cwd),
            created_at=self._now(),
        )
        return self.load_conversation_session(session_id)

    def load_conversation_session(self, session_id: str) -> ConversationSession:
        session = self._store.load_conversation_session(session_id)
        if session is None:
            raise ConversationNotFoundError(f"ConversationSession 不存在: {session_id}")
        return session

    def list_active_branch_entries(
        self,
        *,
        session_id: str,
    ) -> list[ConversationEntry]:
        self.load_conversation_session(session_id)
        return self._store.list_active_branch_entries(session_id=session_id)

    def list_conversation_previews(
        self,
        *,
        limit: int = 20,
        cwd: str | None = None,
        all_sessions: bool = False,
    ) -> list[SessionPreview]:
        normalized_cwd = None if all_sessions else self._normalize_cwd(cwd)
        sessions = self._store.list_conversation_sessions(
            limit=limit,
            cwd=normalized_cwd,
        )
        return [self.build_conversation_preview(session) for session in sessions]

    def build_conversation_preview(
        self,
        session: ConversationSession,
    ) -> SessionPreview:
        return build_conversation_preview(
            session=session,
            entries=self._store.list_active_branch_entries(
                session_id=session.session_id
            ),
        )

    def archive_conversation_session(self, *, session_id: str) -> None:
        self.load_conversation_session(session_id)
        self._store.archive_conversation_session(
            session_id=session_id,
            archived_at=self._now(),
        )

    def delete_conversation_session(self, *, session_id: str) -> None:
        self.load_conversation_session(session_id)
        self._store.delete_conversation_session(session_id=session_id)

    def append_user_message(
        self,
        *,
        session_id: str,
        message: UserMessage,
    ) -> ConversationEntry:
        return self._append_object(
            session_id=session_id,
            object_type="agent_message",
            schema_version=2,
            content=agent_message_to_dict(message),
        )

    def append_assistant_message(
        self,
        *,
        session_id: str,
        message: AssistantMessage,
    ) -> ConversationEntry:
        return self._append_object(
            session_id=session_id,
            object_type="agent_message",
            schema_version=2,
            content=agent_message_to_dict(message),
        )

    def append_tool_result_message(
        self,
        *,
        session_id: str,
        message: ToolResultMessage,
    ) -> ConversationEntry:
        return self._append_object(
            session_id=session_id,
            object_type="agent_message",
            schema_version=2,
            content=agent_message_to_dict(message),
        )

    def append_history_compaction(
        self,
        *,
        session_id: str,
        content: dict[str, Any],
        schema_version: int = 1,
    ) -> ConversationEntry:
        return self._append_object(
            session_id=session_id,
            object_type="history_compaction",
            schema_version=schema_version,
            content=content,
        )

    def append_host_call_request(
        self,
        *,
        session_id: str,
        content: dict[str, Any],
        schema_version: int = 1,
    ) -> ConversationEntry:
        return self._append_object(
            session_id=session_id,
            object_type="host_call_request",
            schema_version=schema_version,
            content=content,
        )

    def append_host_call_response(
        self,
        *,
        session_id: str,
        content: dict[str, Any],
        schema_version: int = 1,
    ) -> ConversationEntry:
        return self._append_object(
            session_id=session_id,
            object_type="host_call_response",
            schema_version=schema_version,
            content=content,
        )

    def move_active_branch_to(
        self,
        *,
        session_id: str,
        node_id: str,
    ) -> ConversationSession:
        session = self.load_conversation_session(session_id)
        active_reference = self._store.find_named_reference(
            session_id=session_id,
            reference_name=ACTIVE_CONVERSATION_REFERENCE,
        )
        transaction = self._store.begin_storage_transaction(
            session_id=session_id,
            expected_sequence=session.current_sequence,
        )
        transaction.move_named_reference(
            reference_name=ACTIVE_CONVERSATION_REFERENCE,
            target_kind="node",
            target_id=node_id,
            expected_current_sequence=(
                active_reference.sequence if active_reference is not None else None
            ),
        )
        transaction.commit()
        return self.load_conversation_session(session_id)

    def _append_object(
        self,
        *,
        session_id: str,
        object_type: str,
        schema_version: int,
        content: dict[str, Any],
    ) -> ConversationEntry:
        session = self.load_conversation_session(session_id)
        active_reference = self._store.find_named_reference(
            session_id=session_id,
            reference_name=ACTIVE_CONVERSATION_REFERENCE,
        )
        if active_reference is not None and active_reference.target_kind != "node":
            raise ValueError("conversation/active 必须指向 ConversationNode")
        transaction = self._store.begin_storage_transaction(
            session_id=session_id,
            expected_sequence=session.current_sequence,
        )
        object_id = transaction.insert_immutable_object(
            object_type=object_type,
            schema_version=schema_version,
            content=content,
        )
        node_id = transaction.append_conversation_node(
            object_id=object_id,
            parent_node_id=(
                active_reference.target_id if active_reference is not None else None
            ),
        )
        transaction.move_named_reference(
            reference_name=ACTIVE_CONVERSATION_REFERENCE,
            target_kind="node",
            target_id=node_id,
            expected_current_sequence=(
                active_reference.sequence if active_reference is not None else None
            ),
        )
        transaction.commit()
        entries = self._store.list_active_branch_entries(session_id=session_id)
        if not entries or entries[-1].node.node_id != node_id:
            raise RuntimeError("提交成功后无法读取新 ConversationEntry")
        return entries[-1]

    @staticmethod
    def _normalize_cwd(cwd: str | None) -> str:
        return (
            str(Path(cwd).resolve()) if cwd is not None else str(Path.cwd().resolve())
        )
