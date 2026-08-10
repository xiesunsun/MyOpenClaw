"""Session 接受的不可变工作身份。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

OperationType = Literal["agent_run"]


@dataclass(frozen=True)
class SessionOperation:
    operation_id: str
    session_id: str
    operation_type: OperationType
    agent_package_version_id: str
    accepted_commit_sequence: int
    created_at: datetime
