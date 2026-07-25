from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from myopenclaw.conversations.repository import SessionRepository
from myopenclaw.conversations.session import Session
from myopenclaw.conversations.session_entry import SessionEntry
from myopenclaw.conversations.session_preview import SessionPreview
from myopenclaw.conversations.session_storage_mapper import build_session_preview
from myopenclaw.integrations.openviking.session_sync import SessionSync


class SessionNotFoundError(LookupError):
    pass


def _normalize_cwd(cwd: str | None) -> str:
    """归一化为绝对路径字符串；None 时取当前工作目录。"""
    if cwd is None:
        return str(Path.cwd().resolve())
    return str(Path(cwd).resolve())


class SessionService:
    """会话生命周期服务。

    OpenViking 同步依赖已从 Session 字段移除；SessionSync 调用点保留，
    第一版由 NoopSessionSync / 空实现承接，完整恢复见 Task 12。
    """

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

    def start(self, *, agent_id: str, cwd: str | None = None) -> Session:
        now = self._now()
        session = Session.create(
            agent_id=agent_id,
            session_id=self._session_id_factory(),
            created_at=now,
            cwd=_normalize_cwd(cwd),
        )
        self._repository.create(session)
        return session

    def resume(self, *, session_id: str) -> Session:
        session = self._repository.load(session_id)
        if session is None:
            raise SessionNotFoundError(f"Session not found: {session_id}")
        return session

    def list_sessions(
        self,
        *,
        limit: int = 20,
        cwd: str | None = None,
        all_sessions: bool = False,
    ) -> list[SessionPreview]:
        """列出会话预览。

        默认按当前（或显式）cwd 过滤；all_sessions=True 时不过滤。
        """
        if all_sessions:
            return self._repository.list(limit=limit, cwd=None)
        return self._repository.list(limit=limit, cwd=_normalize_cwd(cwd))

    def build_preview(self, *, session: Session) -> SessionPreview:
        return build_session_preview(session=session)

    def flush_new_entries(
        self,
        *,
        session: Session,
        entries: list[SessionEntry],
    ) -> None:
        """将自上次 flush 以来新增的 entries 原子落库，并刷新封面元数据。"""
        updated_at = self._now()
        session.touch(at=updated_at)
        if entries:
            self._repository.append_entries(
                session_id=session.session_id,
                entries=entries,
                leaf_id=session.leaf_id,
                updated_at=updated_at,
            )
        # Task 12：OpenViking 游标不再挂在 Session 上；此处保留调用点，默认 noop。
        self._session_sync.sync_pending_messages(session=session)
        self._repository.update_metadata(session)

    def close(self, *, session: Session) -> None:
        updated_at = self._now()
        session.status = "archived"
        session.touch(at=updated_at)
        self._repository.mark_closed(
            session_id=session.session_id,
            updated_at=updated_at,
        )
        self._session_sync.sync_pending_messages(session=session)
        self._session_sync.commit_pending_messages(session=session, force=True)
        self._repository.update_metadata(session)

    def delete(self, *, session_id: str) -> None:
        session = self._repository.load(session_id)
        if session is None:
            raise SessionNotFoundError(f"Session not found: {session_id}")
        self._session_sync.delete_session(session=session)
        self._repository.delete(session_id=session_id)
