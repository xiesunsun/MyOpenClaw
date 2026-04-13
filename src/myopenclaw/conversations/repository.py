from __future__ import annotations

from datetime import datetime
from typing import Protocol

from myopenclaw.conversations.message import SessionMessage
from myopenclaw.conversations.session import Session
from myopenclaw.conversations.session_preview import SessionPreview


class SessionRepository(Protocol):
    def create(self, session: Session) -> None: ...

    def load(self, session_id: str) -> Session | None: ...

    def list(self, *, limit: int = 20) -> list[SessionPreview]: ...

    def append_messages(
        self,
        *,
        session_id: str,
        start_index: int,
        messages: list[SessionMessage],
        updated_at: datetime,
    ) -> None: ...

    def update_metadata(self, session: Session) -> None: ...

    def mark_closed(self, *, session_id: str, updated_at: datetime) -> None: ...
