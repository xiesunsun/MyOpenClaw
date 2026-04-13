from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


def _normalize_whitespace(value: str) -> str:
    return " ".join(value.split())


def _truncate(value: str, *, limit: int = 50) -> str:
    if len(value) <= limit:
        return value
    return f"{value[:limit]}..."


@dataclass(frozen=True)
class SessionPreview:
    session_id: str
    agent_id: str
    created_at: datetime
    updated_at: datetime
    status: str
    message_count: int
    last_message: str

    def __post_init__(self) -> None:
        normalized = _truncate(_normalize_whitespace(self.last_message))
        object.__setattr__(self, "last_message", normalized)
