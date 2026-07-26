from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from pickel.conversations.agent_message import AssistantMessage
from pickel.conversations.session import Session
from pickel.extensions.openviking.openviking_state import OpenVikingSessionState
from pickel.extensions.openviking.session_messages import list_syncable_agent_messages


class CommitPolicy(Protocol):
    def should_commit(
        self,
        *,
        session: Session,
        state: OpenVikingSessionState,
        now: datetime,
    ) -> bool: ...


@dataclass(frozen=True)
class ThresholdCommitPolicy:
    commit_after: timedelta
    commit_after_turns: int

    def should_commit(
        self,
        *,
        session: Session,
        state: OpenVikingSessionState,
        now: datetime,
    ) -> bool:
        if not state.has_pending_remote_commit():
            return False
        if (
            state.last_committed_at is not None
            and now - state.last_committed_at >= self.commit_after
        ):
            return True
        return self._assistant_messages_since_commit(session, state) >= self.commit_after_turns

    def _assistant_messages_since_commit(
        self,
        session: Session,
        state: OpenVikingSessionState,
    ) -> int:
        start_index = (
            0
            if state.last_committed_message_index is None
            else state.last_committed_message_index + 1
        )
        end_index = state.last_synced_message_index
        if end_index is None:
            return 0
        messages = list_syncable_agent_messages(session)[start_index : end_index + 1]
        return sum(1 for message in messages if isinstance(message, AssistantMessage))
