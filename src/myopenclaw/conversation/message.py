from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from myopenclaw.conversation.metadata import MessageMetadata


class MessageRole(str, Enum):
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, object]
    thought_signature: Optional[bytes] = None


@dataclass
class SessionMessage:
    role: MessageRole
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: Optional[str] = None
    tool_name: Optional[str] = None
    is_error: bool = False
    tool_result_metadata: dict[str, Any] = field(default_factory=dict)
    metadata: Optional[MessageMetadata] = None
