from dataclasses import dataclass
from enum import StrEnum

from myopenclaw.conversation.metadata import MessageMetadata


class MessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"


@dataclass
class Message:
    role: MessageRole
    text: str
    metadata: MessageMetadata | None = None
