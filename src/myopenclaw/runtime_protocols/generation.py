from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from myopenclaw.conversation.message import SessionMessage, ToolCall
from myopenclaw.conversation.metadata import MessageMetadata
from myopenclaw.tools.base import ToolSpec


@dataclass
class TokenUsage:
    input_tokens: int | None = None
    output_tokens: int | None = None


class FinishReason(StrEnum):
    STOP = "stop"
    TOOL_CALLS = "tool_calls"
    MAX_STEPS = "max_steps"


@dataclass
class GenerateRequest:
    system_instruction: str | None
    messages: list[SessionMessage]
    tools: list[ToolSpec] = field(default_factory=list)


@dataclass
class GenerateResult:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: FinishReason = FinishReason.STOP
    usage: TokenUsage | None = None
    metadata: MessageMetadata | None = None
    raw: Any | None = None
