from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class MessageMetadata:
    provider: str
    model: str
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    total_tokens: Optional[int] = None
    elapsed_ms: Optional[int] = None
    provider_finish_reason: Optional[str] = None
    provider_finish_message: Optional[str] = None
    provider_response_id: Optional[str] = None
    provider_model_version: Optional[str] = None
