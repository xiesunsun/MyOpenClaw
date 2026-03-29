from dataclasses import dataclass, field
from enum import StrEnum

from myopenclaw.conversation.metadata import MessageMetadata


class MessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, object]
    thought_signature: bytes | None = None


@dataclass
class SessionMessage:
    role: MessageRole
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str | None = None
    tool_name: str | None = None
    is_error: bool = False
    metadata: MessageMetadata | None = None
