"""Session：内存中的 entry 树 + leaf_id 活动指针。

不再使用线性 messages，也不再携带 OpenViking 同步字段（见 Query-Context-Harness 设计）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4

from myopenclaw.conversations.agent_message import (
    AssistantMessage,
    ToolResultMessage,
    UserMessage,
    agent_message_to_dict,
)
from myopenclaw.conversations.session_entry import (
    COMPACTION_PAYLOAD_VERSION,
    ENTRY_TYPE_COMPACTION,
    ENTRY_TYPE_MESSAGE,
    SessionEntry,
)


@dataclass
class Session:
    session_id: str
    agent_id: str
    cwd: str = ""
    leaf_id: str | None = None
    entries: list[SessionEntry] = field(default_factory=list)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    status: str = "active"
    title: str | None = None

    @classmethod
    def create(
        cls,
        agent_id: str,
        cwd: str = "",
        session_id: Optional[str] = None,
        created_at: datetime | None = None,
    ) -> "Session":
        now = created_at or datetime.now(timezone.utc)
        return cls(
            session_id=session_id or str(uuid4()),
            agent_id=agent_id,
            cwd=cwd,
            created_at=now,
            updated_at=now,
        )

    def touch(self, *, at: datetime | None = None) -> None:
        self.updated_at = at or datetime.now(timezone.utc)

    def _entry_map(self) -> dict[str, SessionEntry]:
        return {entry.entry_id: entry for entry in self.entries}

    def active_path(self) -> list[SessionEntry]:
        """从 leaf 沿 parent_id 回溯，返回根 → leaf 的路径。"""
        if self.leaf_id is None:
            return []

        by_id = self._entry_map()
        path: list[SessionEntry] = []
        current_id: str | None = self.leaf_id
        seen: set[str] = set()

        while current_id is not None:
            if current_id in seen:
                raise ValueError("active_path 检测到 parent 环")
            seen.add(current_id)
            entry = by_id.get(current_id)
            if entry is None:
                raise ValueError(f"leaf/parent 指向不存在的 entry: {current_id}")
            if entry.session_id != self.session_id:
                raise ValueError(f"entry 不属于本 session: {current_id}")
            path.append(entry)
            current_id = entry.parent_id

        path.reverse()
        return path

    def append_user(self, message: UserMessage) -> SessionEntry:
        return self._append_message(message)

    def append_assistant(self, message: AssistantMessage) -> SessionEntry:
        return self._append_message(message)

    def append_tool_result(self, message: ToolResultMessage) -> SessionEntry:
        return self._append_message(message)

    def append_compaction(self, payload: dict[str, Any]) -> SessionEntry:
        """追加 compaction entry；payload 需为 JSON-ready dict。"""
        if not isinstance(payload, dict):
            raise TypeError("compaction payload 必须是 dict")
        stored = dict(payload)
        stored.setdefault("payload_version", COMPACTION_PAYLOAD_VERSION)
        return self._append_entry(ENTRY_TYPE_COMPACTION, stored)

    def move_leaf(self, entry_id: str) -> None:
        """将活动指针移到本 session 已有 entry（切分支，不写新 entry）。"""
        if entry_id not in self._entry_map():
            raise ValueError(f"move_leaf 目标 entry 不存在: {entry_id}")
        self.leaf_id = entry_id
        self.touch()

    def _append_message(
        self,
        message: UserMessage | AssistantMessage | ToolResultMessage,
    ) -> SessionEntry:
        return self._append_entry(ENTRY_TYPE_MESSAGE, agent_message_to_dict(message))

    def _append_entry(self, entry_type: str, payload: dict[str, Any]) -> SessionEntry:
        now = datetime.now(timezone.utc)
        entry = SessionEntry(
            entry_id=str(uuid4()),
            session_id=self.session_id,
            parent_id=self.leaf_id,
            entry_type=entry_type,
            payload=payload,
            created_at=now,
        )
        self.entries.append(entry)
        self.leaf_id = entry.entry_id
        self.touch(at=now)
        return entry
