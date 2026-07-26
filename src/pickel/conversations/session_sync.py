"""会话同步协议与组合器。

协议定义在 core：core 的 SessionService 不应从集成层 import 自己的协议。
具体实现（如 OpenViking）由 extension 提供。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Protocol

from pickel.conversations.session import Session

logger = logging.getLogger(__name__)


class SessionSync(Protocol):
    def sync_pending_messages(self, *, session: Session) -> None: ...

    def commit_pending_messages(
        self,
        *,
        session: Session,
        force: bool = False,
    ) -> None: ...

    def delete_session(self, *, session: Session) -> None: ...


class NoopSessionSync:
    def sync_pending_messages(self, *, session: Session) -> None:
        return None

    def commit_pending_messages(
        self,
        *,
        session: Session,
        force: bool = False,
    ) -> None:
        return None

    def delete_session(self, *, session: Session) -> None:
        return None


class CompositeSessionSync:
    """把多个 extension 贡献的 sync 串起来。

    单个 sync 失败只记日志，不影响其余 sync，也不打断会话主流程 ——
    同步是旁路能力，不该让一个坏 extension 弄挂对话。
    """

    def __init__(self, syncs: Sequence[SessionSync]) -> None:
        self._syncs = list(syncs)

    def sync_pending_messages(self, *, session: Session) -> None:
        for sync in self._syncs:
            self._safe_call(sync, "sync_pending_messages", session=session)

    def commit_pending_messages(
        self,
        *,
        session: Session,
        force: bool = False,
    ) -> None:
        for sync in self._syncs:
            self._safe_call(
                sync,
                "commit_pending_messages",
                session=session,
                force=force,
            )

    def delete_session(self, *, session: Session) -> None:
        for sync in self._syncs:
            self._safe_call(sync, "delete_session", session=session)

    @staticmethod
    def _safe_call(sync: SessionSync, method: str, **kwargs) -> None:
        try:
            getattr(sync, method)(**kwargs)
        except Exception:
            logger.exception(
                "Session sync %s.%s failed",
                type(sync).__name__,
                method,
            )
