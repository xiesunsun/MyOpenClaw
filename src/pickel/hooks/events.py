"""Hook 事件 DTO（只读快照）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class HookEventBase:
    event_id: str = field(default_factory=lambda: str(uuid4()))
    session_id: str = ""
    turn_id: str = ""
    step_index: int | None = None
    occurred_at: datetime = field(default_factory=_now)


@dataclass(frozen=True)
class UserPromptSubmitEvent(HookEventBase):
    prompt: str = ""


@dataclass(frozen=True)
class PreToolUseEvent(HookEventBase):
    tool_name: str = ""
    tool_call_id: str = ""
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PostToolUseEvent(HookEventBase):
    tool_name: str = ""
    tool_call_id: str = ""
    arguments: dict[str, Any] = field(default_factory=dict)
    result_content: str = ""
    is_error: bool = False


@dataclass(frozen=True)
class PostToolBatchEvent(HookEventBase):
    outcomes: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class TurnEndEvent(HookEventBase):
    reason: str = "completed"
