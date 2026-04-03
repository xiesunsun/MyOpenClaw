from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from myopenclaw.domain.metadata import MessageMetadata


class MessageRole(str, Enum):
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
class ToolCallResult:
    call_id: str
    content: str
    is_error: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolCallBatch:
    batch_id: str
    step_index: int
    calls: list[ToolCall] = field(default_factory=list)
    results: list[ToolCallResult] = field(default_factory=list)


@dataclass
class SessionMessage:
    role: MessageRole
    content: str = ""
    metadata: MessageMetadata | None = None
    tool_call_batch: ToolCallBatch | None = None
