from __future__ import annotations

from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Any, Optional
from uuid import uuid4

from myopenclaw.conversations.message import (
    MessageRole,
    SessionMessage,
    ToolCallBatch,
)
from myopenclaw.conversations.metadata import MessageMetadata


@dataclass
class Session:
    session_id: str
    agent_id: str
    messages: list[SessionMessage] = field(default_factory=list)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    status: str = "active"
    remote_session_id: str | None = None
    last_synced_message_index: int | None = None
    last_committed_message_index: int | None = None
    last_committed_at: datetime | None = None
    openviking_account_id: str | None = None
    openviking_user_id: str | None = None
    openviking_agent_id: str | None = None

    @classmethod
    def create(
        cls,
        agent_id: str,
        session_id: Optional[str] = None,
        created_at: datetime | None = None,
    ) -> "Session":
        now = created_at or datetime.now(timezone.utc)
        return cls(
            session_id=session_id or str(uuid4()),
            agent_id=agent_id,
            created_at=now,
            updated_at=now,
        )

    def touch(self, *, at: datetime | None = None) -> None:
        self.updated_at = at or datetime.now(timezone.utc)

    def bind_openviking(self, account_id: str, user_id: str, agent_id: str) -> None:
        self.openviking_account_id = account_id
        self.openviking_user_id = user_id
        self.openviking_agent_id = agent_id

    def pending_sync_start_index(self) -> int:
        if self.last_synced_message_index is None:
            return 0
        return self.last_synced_message_index + 1

    def pending_sync_messages(self) -> list[SessionMessage]:
        return self.messages[self.pending_sync_start_index() :]

    def has_pending_remote_commit(self) -> bool:
        if self.last_synced_message_index is None:
            return False
        if self.last_committed_message_index is None:
            return True
        return self.last_committed_message_index < self.last_synced_message_index

    def mark_messages_synced(
        self,
        *,
        remote_session_id: str,
        last_message_index: int,
    ) -> None:
        self.remote_session_id = remote_session_id
        self.last_synced_message_index = last_message_index
        if (
            self.last_committed_message_index is not None
            and self.last_committed_message_index > last_message_index
        ):
            raise ValueError(
                "last_committed_message_index cannot exceed last_synced_message_index"
            )

    def mark_messages_committed(
        self,
        *,
        last_message_index: int,
        committed_at: datetime,
    ) -> None:
        if (
            self.last_synced_message_index is not None
            and last_message_index > self.last_synced_message_index
        ):
            raise ValueError(
                "last_committed_message_index cannot exceed last_synced_message_index"
            )
        self.last_committed_message_index = last_message_index
        self.last_committed_at = committed_at

    def append_user_message(self, content: str) -> None:
        self.messages.append(SessionMessage(role=MessageRole.USER, content=content))

    def append_assistant_message(
        self,
        content: str = "",
        metadata: Optional[MessageMetadata] = None,
        tool_call_batch: Optional[ToolCallBatch] = None,
        provider_thinking_blocks: list[dict[str, Any]] | None = None,
    ) -> None:
        self.messages.append(
            SessionMessage(
                role=MessageRole.ASSISTANT,
                content=content,
                metadata=metadata,
                tool_call_batch=tool_call_batch,
                provider_thinking_blocks=provider_thinking_blocks,
            )
        )

    def append_assistant_tool_batch(
        self,
        batch: ToolCallBatch,
        *,
        content: str = "",
        metadata: Optional[MessageMetadata] = None,
        provider_thinking_blocks: list[dict[str, Any]] | None = None,
    ) -> None:
        self.append_assistant_message(
            content=content,
            metadata=metadata,
            tool_call_batch=batch,
            provider_thinking_blocks=provider_thinking_blocks,
        )
