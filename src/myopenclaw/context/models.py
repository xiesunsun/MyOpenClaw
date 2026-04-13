from __future__ import annotations

from dataclasses import dataclass, field

from myopenclaw.conversations.message import MessageRole, SessionMessage


@dataclass(frozen=True)
class UserTurn:
    user_message: SessionMessage
    assistant_messages: list[SessionMessage] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.user_message.role != MessageRole.USER:
            raise ValueError("UserTurn.user_message must have role 'user'.")
        if any(message.role != MessageRole.ASSISTANT for message in self.assistant_messages):
            raise ValueError("UserTurn.assistant_messages must all have role 'assistant'.")
