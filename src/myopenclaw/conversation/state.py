from dataclasses import dataclass, field

from myopenclaw.conversation.message import Message, MessageRole


@dataclass
class SessionState:
    session_id: str
    messages: list[Message] = field(default_factory=list)

    def add_user_message(self, text: str) -> None:
        self.messages.append(Message(role=MessageRole.USER, text=text))

    def add_assistant_message(self, text: str) -> None:
        self.messages.append(Message(role=MessageRole.ASSISTANT, text=text))
