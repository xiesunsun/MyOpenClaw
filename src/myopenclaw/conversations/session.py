from __future__ import annotations

from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Optional
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
    last_committed_at: datetime | None = None

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

    def append_user_message(self, content: str) -> None:
        self.messages.append(SessionMessage(role=MessageRole.USER, content=content))

    def append_assistant_message(
        self,
        content: str = "",
        metadata: Optional[MessageMetadata] = None,
        tool_call_batch: Optional[ToolCallBatch] = None,
    ) -> None:
        self.messages.append(
            SessionMessage(
                role=MessageRole.ASSISTANT,
                content=content,
                metadata=metadata,
                tool_call_batch=tool_call_batch,
            )
        )

    def append_assistant_tool_batch(
        self,
        batch: ToolCallBatch,
        *,
        content: str = "",
        metadata: Optional[MessageMetadata] = None,
    ) -> None:
        self.append_assistant_message(
            content=content,
            metadata=metadata,
            tool_call_batch=batch,
        )
