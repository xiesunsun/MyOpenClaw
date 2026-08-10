"""会话身份与当前持久化提交的只读视图。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class ConversationSession:
    session_id: str
    agent_id: str
    cwd: str
    current_commit_sequence: int
    active_node_id: str | None
    created_at: datetime
    updated_at: datetime
    status: str = "active"
    title: str | None = None
