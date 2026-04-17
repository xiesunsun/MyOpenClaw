from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Protocol

from myopenclaw.conversations.session import Session
from myopenclaw.integrations.openviking.commit_policy import CommitPolicy
from myopenclaw.integrations.openviking.config import OpenVikingConfig
from myopenclaw.integrations.openviking.session_client import OpenVikingSessionClient
from myopenclaw.integrations.openviking.session_message_mapper import SessionMessageMapper


LOGGER = logging.getLogger(__name__)
LOGGER.addHandler(logging.NullHandler())


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


class OpenVikingSessionSync:
    def __init__(
        self,
        *,
        config: OpenVikingConfig,
        remote_agent_id: str,
        client: OpenVikingSessionClient,
        message_mapper: SessionMessageMapper,
        commit_policy: CommitPolicy,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._config = config
        self._remote_agent_id = remote_agent_id
        self._client = client
        self._message_mapper = message_mapper
        self._commit_policy = commit_policy
        self._now = now or (lambda: datetime.now(timezone.utc))

    def sync_pending_messages(self, *, session: Session) -> None:
        session.bind_openviking(
            account_id=self._config.account_id,
            user_id=self._config.user_id,
            agent_id=self._remote_agent_id,
        )
        start_index = session.pending_sync_start_index()
        pending_messages = session.pending_sync_messages()
        try:
            if pending_messages:
                remote_session_id = self._client.ensure_session(
                    session_id=session.session_id
                )
                for offset, message in enumerate(pending_messages):
                    payload = self._message_mapper.to_openviking_message(message)
                    self._client.append_message(
                        session_id=remote_session_id,
                        role=payload.role,
                        content=payload.content,
                        parts=payload.parts,
                    )
                    session.mark_messages_synced(
                        remote_session_id=remote_session_id,
                        last_message_index=start_index + offset,
                    )
            if self._commit_policy.should_commit(session=session, now=self._now()):
                self.commit_pending_messages(session=session, force=False)
        except Exception as exc:
            LOGGER.warning("OpenViking session sync failed: %s", exc, exc_info=False)

    def commit_pending_messages(
        self,
        *,
        session: Session,
        force: bool = False,
    ) -> None:
        if not session.has_pending_remote_commit():
            return
        if not force and not self._commit_policy.should_commit(
            session=session,
            now=self._now(),
        ):
            return
        try:
            remote_session_id = session.remote_session_id or self._client.ensure_session(
                session_id=session.session_id
            )
            self._client.commit_session(session_id=remote_session_id)
            if session.last_synced_message_index is not None:
                session.mark_messages_committed(
                    last_message_index=session.last_synced_message_index,
                    committed_at=self._now(),
                )
        except Exception as exc:
            LOGGER.warning("OpenViking session commit failed: %s", exc, exc_info=False)

    def delete_session(self, *, session: Session) -> None:
        remote_session_id = session.remote_session_id or session.session_id
        self._client.delete_session(session_id=remote_session_id)
