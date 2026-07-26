# src/pickel/shared/event_envelope.py
"""事件信封：hook 与 runtime 事件共用的身份字段。

放在 shared/ 而非 runs/：runs 依赖 hooks（react 调 lifecycle_hooks），
hooks 若反向依赖 runs 会形成循环。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from uuid import uuid4


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class EventIdentity:
    """一个事件是谁、属于哪个 turn、发生在何时。"""

    event_id: str = field(default_factory=lambda: str(uuid4()))
    session_id: str = ""
    turn_id: str = ""
    step_index: int | None = None
    occurred_at: datetime = field(default_factory=_now)


@dataclass(frozen=True)
class EventEnvelope(EventIdentity):
    """runtime 事件的信封：身份 + 全序序号。

    seq 由 EventBus 统一分配（红线 4）；-1 表示尚未进入 bus。
    hook 事件不经过 bus，故只用 EventIdentity，不背这个字段。
    """

    seq: int = -1

    def with_seq(self, seq: int) -> "EventEnvelope":
        return replace(self, seq=seq)
