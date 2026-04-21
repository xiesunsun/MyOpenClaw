from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from myopenclaw.conversations.message import SessionMessage, ToolCall
    from myopenclaw.conversations.metadata import MessageMetadata
    from myopenclaw.tools.base import ToolSpec


@dataclass
class TokenUsage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_content_tokens: int | None = None
    thoughts_tokens: int | None = None
    tool_use_prompt_tokens: int | None = None
    total_tokens: int | None = None


class FinishReason(StrEnum):
    STOP = "stop"
    TOOL_CALLS = "tool_calls"
    MAX_STEPS = "max_steps"


@dataclass
class GenerateRequest:
    system_instruction: str | None
    messages: list["SessionMessage"]
    tools: list["ToolSpec"] = field(default_factory=list)


@dataclass
class GenerateResult:
    text: str = ""
    tool_calls: list["ToolCall"] = field(default_factory=list)
    finish_reason: FinishReason = FinishReason.STOP
    provider_finish_reason: str | None = None
    provider_finish_message: str | None = None
    provider_response_id: str | None = None
    provider_model_version: str | None = None
    usage: TokenUsage | None = None
    metadata: "MessageMetadata" | None = None
    provider_thinking_blocks: list[dict[str, Any]] | None = None
    raw: Any | None = None
