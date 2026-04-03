from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional
from uuid import uuid4

from myopenclaw.conversation.message import (
    MessageRole,
    SessionMessage,
    ToolCallBatch,
)
from myopenclaw.conversation.metadata import MessageMetadata


@dataclass
class Session:
    session_id: str
    agent_id: str
    messages: list[SessionMessage] = field(default_factory=list)

    @classmethod
    def create(cls, agent_id: str, session_id: Optional[str] = None) -> "Session":
        return cls(
            session_id=session_id or str(uuid4()),
            agent_id=agent_id,
        )

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
