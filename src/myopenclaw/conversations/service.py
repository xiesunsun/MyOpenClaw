from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from uuid import uuid4

from myopenclaw.conversations.repository import SessionRepository
from myopenclaw.conversations.session import Session
from myopenclaw.conversations.session_preview import SessionPreview
from myopenclaw.conversations.session_storage_mapper import build_session_preview
from myopenclaw.integrations.openviking.session_sync import SessionSync


class SessionNotFoundError(LookupError):
    pass


class SessionService:
    def __init__(
        self,
        repository: SessionRepository,
        session_sync: SessionSync,
        *,
        session_id_factory: Callable[[], str] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._repository = repository
        self._session_sync = session_sync
        self._session_id_factory = session_id_factory or (lambda: str(uuid4()))
        self._now = now or (lambda: datetime.now(timezone.utc))

    def start(self, *, agent_id: str) -> Session:
        now = self._now()
        session = Session.create(
            agent_id=agent_id,
            session_id=self._session_id_factory(),
            created_at=now,
        )
        self._repository.create(session)
        return session

    def resume(self, *, session_id: str) -> Session:
        session = self._repository.load(session_id)
        if session is None:
            raise SessionNotFoundError(f"Session not found: {session_id}")
        return session

    def list_sessions(self, *, limit: int = 20) -> list[SessionPreview]:
        return self._repository.list(limit=limit)

    def build_preview(self, *, session: Session) -> SessionPreview:
        return build_session_preview(session=session)

    def flush_new_messages(self, *, session: Session, start_index: int) -> None:
        updated_at = self._now()
        session.touch(at=updated_at)
        new_messages = session.messages[start_index:]
        if new_messages:
            self._repository.append_messages(
                session_id=session.session_id,
                start_index=start_index,
                messages=new_messages,
                updated_at=updated_at,
            )
        self._repository.update_metadata(session)
        self._session_sync.sync_new_messages(
            session=session,
            start_index=start_index,
        )

    def close(self, *, session: Session) -> None:
        updated_at = self._now()
        session.status = "closed"
        session.touch(at=updated_at)
        self._repository.mark_closed(
            session_id=session.session_id,
            updated_at=updated_at,
        )
        self._session_sync.commit(session=session)
