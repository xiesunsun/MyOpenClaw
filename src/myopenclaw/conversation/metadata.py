from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class MessageMetadata:
    provider: str
    model: str
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    elapsed_ms: Optional[int] = None
