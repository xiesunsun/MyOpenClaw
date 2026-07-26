from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timezone

from pickel.conversations.session import Session
from pickel.conversations.session_sync import NoopSessionSync, SessionSync
from pickel.integrations.openviking.commit_policy import CommitPolicy
from pickel.integrations.openviking.config import OpenVikingConfig
from pickel.integrations.openviking.openviking_state import (
    InMemoryOpenVikingStateStore,
    OpenVikingSessionState,
    OpenVikingStateStore,
)
from pickel.integrations.openviking.session_client import OpenVikingSessionClient
from pickel.integrations.openviking.session_message_mapper import SessionMessageMapper
from pickel.integrations.openviking.session_messages import (
    agent_message_to_session_message,
    list_syncable_agent_messages,
)


LOGGER = logging.getLogger(__name__)
LOGGER.addHandler(logging.NullHandler())


class OpenVikingSessionSync:
    """OpenViking 同步：状态走旁路 store，消息来自 Session active path。"""

    def __init__(
        self,
        *,
        config: OpenVikingConfig,
        remote_agent_id: str,
        client: OpenVikingSessionClient,
        message_mapper: SessionMessageMapper,
        commit_policy: CommitPolicy,
        state_store: OpenVikingStateStore | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._config = config
        self._remote_agent_id = remote_agent_id
        self._client = client
        self._message_mapper = message_mapper
        self._commit_policy = commit_policy
        self._state_store: OpenVikingStateStore = (
            state_store if state_store is not None else InMemoryOpenVikingStateStore()
        )
        self._now = now or (lambda: datetime.now(timezone.utc))

    def state_for(self, session: Session) -> OpenVikingSessionState:
        return self._state_store.get_or_create(session.session_id)

    def save_state(self, session: Session, state: OpenVikingSessionState | None = None) -> None:
        resolved = state if state is not None else self.state_for(session)
        self._state_store.put_state(session.session_id, resolved)

    def sync_pending_messages(self, *, session: Session) -> None:
        state = self.state_for(session)
        state.bind(
            account_id=self._config.account_id,
            user_id=self._config.user_id,
            agent_id=self._remote_agent_id,
        )
        self.save_state(session, state)

        messages = list_syncable_agent_messages(session)
        start_index = state.pending_sync_start_index()
        pending_messages = messages[start_index:]
        try:
            if pending_messages:
                remote_session_id = self._client.ensure_session(
                    session_id=session.session_id
                )
                for offset, message in enumerate(pending_messages):
                    legacy = agent_message_to_session_message(message)
                    payload = self._message_mapper.to_openviking_message(legacy)
                    self._client.append_message(
                        session_id=remote_session_id,
                        role=payload.role,
                        content=payload.content,
                        parts=payload.parts,
                    )
                    state.mark_messages_synced(
                        remote_session_id=remote_session_id,
                        last_message_index=start_index + offset,
                    )
                    self.save_state(session, state)
            if self._commit_policy.should_commit(
                session=session,
                state=state,
                now=self._now(),
            ):
                self.commit_pending_messages(session=session, force=False)
        except Exception as exc:
            LOGGER.warning("OpenViking session sync failed: %s", exc, exc_info=False)

    def commit_pending_messages(
        self,
        *,
        session: Session,
        force: bool = False,
    ) -> None:
        state = self.state_for(session)
        if not state.has_pending_remote_commit():
            return
        if not force and not self._commit_policy.should_commit(
            session=session,
            state=state,
            now=self._now(),
        ):
            return
        try:
            remote_session_id = state.remote_session_id or self._client.ensure_session(
                session_id=session.session_id
            )
            self._client.commit_session(session_id=remote_session_id)
            if state.last_synced_message_index is not None:
                state.mark_messages_committed(
                    last_message_index=state.last_synced_message_index,
                    committed_at=self._now(),
                )
                self.save_state(session, state)
        except Exception as exc:
            LOGGER.warning("OpenViking session commit failed: %s", exc, exc_info=False)

    def delete_session(self, *, session: Session) -> None:
        state = self._state_store.get_state(session.session_id)
        remote_session_id = (
            state.remote_session_id if state is not None else None
        ) or session.session_id
        self._client.delete_session(session_id=remote_session_id)
