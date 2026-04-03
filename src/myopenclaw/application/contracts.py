from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from myopenclaw.domain.metadata import MessageMetadata


class ModelMessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None = None


@dataclass(frozen=True)
class ModelToolCall:
    id: str
    name: str
    arguments: dict[str, object]
    thought_signature: bytes | None = None


@dataclass
class ModelToolResult:
    call_id: str
    content: str
    is_error: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ModelToolCallBatch:
    batch_id: str
    step_index: int
    calls: list[ModelToolCall] = field(default_factory=list)
    results: list[ModelToolResult] = field(default_factory=list)


@dataclass
class ModelMessage:
    role: ModelMessageRole
    content: str = ""
    tool_call_batch: ModelToolCallBatch | None = None


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
    messages: list[ModelMessage]
    tools: list[ToolSpec] = field(default_factory=list)


@dataclass
class GenerateResult:
    text: str = ""
    tool_calls: list[ModelToolCall] = field(default_factory=list)
    finish_reason: FinishReason = FinishReason.STOP
    provider_finish_reason: str | None = None
    provider_finish_message: str | None = None
    provider_response_id: str | None = None
    provider_model_version: str | None = None
    usage: TokenUsage | None = None
    metadata: MessageMetadata | None = None
    raw: Any | None = None


@dataclass
class TurnResult:
    text: str = ""
    metadata: MessageMetadata | None = None
    finish_reason: FinishReason = FinishReason.STOP
    tool_batches: list[ModelToolCallBatch] = field(default_factory=list)
    message_count: int = 0
