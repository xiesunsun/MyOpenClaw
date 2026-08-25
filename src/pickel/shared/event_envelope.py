# src/pickel/shared/event_envelope.py
"""Hook 与 Runtime Event 共用的 Operation 身份字段。"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from uuid import uuid4

from pickel.shared.execution_identity import ExecutionIdentity


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class EventEnvelope:
    """Runtime 事件元数据；执行位置只由 ExecutionIdentity 表达。

    event_sequence 由 EventBus 统一分配；-1 表示尚未进入 bus。
    """

    event_id: str = field(default_factory=lambda: str(uuid4()))
    identity: ExecutionIdentity = field(default_factory=ExecutionIdentity)
    occurred_at: datetime = field(default_factory=_now)
    event_sequence: int = -1

    def with_event_sequence(self, event_sequence: int) -> "EventEnvelope":
        return replace(self, event_sequence=event_sequence)
