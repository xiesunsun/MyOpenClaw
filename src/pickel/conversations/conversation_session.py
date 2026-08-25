"""ConversationSession 持久化实体。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

TitleSource = Literal["generated", "user"]


@dataclass(frozen=True)
class ConversationSession:
    session_id: str
    agent_id: str
    workspace_id: str
    cwd: Path
    active_node_id: str | None
    active_operation_id: str | None
    title: str | None
    title_source: TitleSource | None
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None

    def __post_init__(self) -> None:
        for name in ("session_id", "agent_id", "workspace_id"):
            if not getattr(self, name):
                raise ValueError(f"{name} 不能为空")
        cwd = Path(self.cwd).expanduser().resolve(strict=False)
        if not cwd.is_absolute():
            raise ValueError("cwd 必须是绝对路径")
        if (self.title is None) != (self.title_source is None):
            raise ValueError("title 与 title_source 必须同时为空或同时存在")
        if self.title_source not in (None, "generated", "user"):
            raise ValueError(f"不支持的 title_source: {self.title_source!r}")
        if self.archived_at is not None and self.active_operation_id is not None:
            raise ValueError("归档 Session 不能有 active_operation_id")
        object.__setattr__(self, "cwd", cwd)
