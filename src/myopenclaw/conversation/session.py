from dataclasses import dataclass, field
from uuid import uuid4

from myopenclaw.conversation.message import MessageRole, SessionMessage, ToolCall
from myopenclaw.conversation.metadata import MessageMetadata


@dataclass
class Session:
    session_id: str
    agent_id: str
    messages: list[SessionMessage] = field(default_factory=list)

    @classmethod
    def create(cls, agent_id: str, session_id: str | None = None) -> "Session":
        return cls(
            session_id=session_id or str(uuid4()),
            agent_id=agent_id,
        )

    def append_user_message(self, content: str) -> None:
        self.messages.append(SessionMessage(role=MessageRole.USER, content=content))

    def append_assistant_message(
        self,
        content: str = "",
        metadata: MessageMetadata | None = None,
        tool_calls: list[ToolCall] | None = None,
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
    ) -> None:
        self.messages.append(
            SessionMessage(
                role=MessageRole.TOOL,
                content=content,
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                is_error=is_error,
            )
        )
