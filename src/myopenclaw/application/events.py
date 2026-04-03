from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Awaitable, Callable, TypeAlias

from myopenclaw.domain.metadata import MessageMetadata


class RuntimeEventType(StrEnum):
    MODEL_STEP_STARTED = "model_step_started"
    TOOL_CALL_STARTED = "tool_call_started"
    TOOL_CALL_COMPLETED = "tool_call_completed"
    TOOL_CALL_FAILED = "tool_call_failed"
    ASSISTANT_MESSAGE = "assistant_message"


@dataclass(frozen=True)
class ToolCallView:
    id: str
    name: str
    arguments: dict[str, object]


@dataclass(frozen=True)
class ToolResultView:
    content: str
    is_error: bool = False
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class RuntimeEvent:
    event_type: RuntimeEventType
    step_index: int | None = None
    batch_id: str | None = None
    call_index: int | None = None
    total_calls: int | None = None
    tool_call: ToolCallView | None = None
    tool_result: ToolResultView | None = None
    text: str = ""
    metadata: MessageMetadata | None = None


RuntimeEventHandler: TypeAlias = Callable[[RuntimeEvent], Awaitable[None] | None]
