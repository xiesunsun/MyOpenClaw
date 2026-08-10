"""可通过追加版本移动的持久化引用。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ReferenceTargetKind = Literal["node", "object"]


@dataclass(frozen=True)
class NamedReference:
    session_id: str
    reference_name: str
    sequence: int
    target_kind: ReferenceTargetKind
    target_id: str
