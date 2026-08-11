# src/pickel/shared/event_envelope.py
"""Hook 与 Runtime Event 共用的 Operation 身份字段。"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from uuid import uuid4


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class EventIdentity:
    """一个事件属于哪个 Operation/ModelStep、发生在何时。"""

    event_id: str = field(default_factory=lambda: str(uuid4()))
    session_id: str = ""
    operation_id: str = ""
    step_id: str | None = None
    step_sequence: int | None = None
    occurred_at: datetime = field(default_factory=_now)


@dataclass(frozen=True)
class EventEnvelope(EventIdentity):
    """runtime 事件的信封：身份 + 全序序号。

    event_sequence 由 EventBus 统一分配；-1 表示尚未进入 bus。
    hook 事件不经过 bus，故只用 EventIdentity，不背这个字段。
    """

    event_sequence: int = -1

    def with_event_sequence(self, event_sequence: int) -> "EventEnvelope":
        return replace(self, event_sequence=event_sequence)
