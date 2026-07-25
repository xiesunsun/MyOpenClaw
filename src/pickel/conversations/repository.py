from __future__ import annotations

from datetime import datetime
from typing import Protocol

from pickel.conversations.session import Session
from pickel.conversations.session_entry import SessionEntry
from pickel.conversations.session_preview import SessionPreview


class SessionRepository(Protocol):
    def create(self, session: Session) -> None: ...

    def load(self, session_id: str) -> Session | None: ...

    def list(
        self, *, limit: int = 20, cwd: str | None = None
    ) -> list[SessionPreview]: ...

    def append_entries(
        self,
        *,
        session_id: str,
        entries: list[SessionEntry],
        leaf_id: str | None,
        updated_at: datetime,
    ) -> None: ...

    def update_metadata(self, session: Session) -> None: ...

    def mark_closed(self, *, session_id: str, updated_at: datetime) -> None: ...

    def delete(self, *, session_id: str) -> None: ...
