from __future__ import annotations

from dataclasses import dataclass


@dataclass
class MessageMetadata:
    provider: str
    model: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    elapsed_ms: int | None = None
    provider_finish_reason: str | None = None
    provider_finish_message: str | None = None
    provider_response_id: str | None = None
    provider_model_version: str | None = None
