from __future__ import annotations

from typing import Protocol

from myopenclaw.conversations.session import Session


class SessionSync(Protocol):
    def sync_new_messages(self, *, session: Session, start_index: int) -> None: ...

    def commit(self, *, session: Session) -> None: ...


class NoopSessionSync:
    def sync_new_messages(self, *, session: Session, start_index: int) -> None:
        return None

    def commit(self, *, session: Session) -> None:
        return None
