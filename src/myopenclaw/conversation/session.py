from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional
from uuid import uuid4

from myopenclaw.conversation.message import MessageRole, SessionMessage, ToolCall
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
        tool_calls: Optional[list[ToolCall]] = None,
    ) -> None:
        self.messages.append(
            SessionMessage(
                role=MessageRole.ASSISTANT,
                content=content,
                tool_calls=list(tool_calls or []),
                metadata=metadata,
            )
        )

    def append_tool_result(
        self,
        content: str,
        tool_call_id: str,
        tool_name: str,
        *,
        is_error: bool = False,
        metadata: Optional[dict[str, Any]] = None,
    ) -> None:
        self.messages.append(
            SessionMessage(
                role=MessageRole.TOOL,
                content=content,
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                is_error=is_error,
                tool_result_metadata=dict(metadata or {}),
            )
        )
