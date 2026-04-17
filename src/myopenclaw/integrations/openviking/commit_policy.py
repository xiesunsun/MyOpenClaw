from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from myopenclaw.conversations.message import MessageRole
from myopenclaw.conversations.session import Session


class CommitPolicy(Protocol):
    def should_commit(self, *, session: Session, now: datetime) -> bool: ...


@dataclass(frozen=True)
class ThresholdCommitPolicy:
    commit_after: timedelta
    commit_after_turns: int

    def should_commit(self, *, session: Session, now: datetime) -> bool:
        if not session.has_pending_remote_commit():
            return False
        if (
            session.last_committed_at is not None
            and now - session.last_committed_at >= self.commit_after
        ):
            return True
        return self._assistant_messages_since_commit(session) >= self.commit_after_turns

    def _assistant_messages_since_commit(self, session: Session) -> int:
        start_index = (
            0
            if session.last_committed_message_index is None
            else session.last_committed_message_index + 1
        )
        end_index = session.last_synced_message_index
        if end_index is None:
            return 0
        messages = session.messages[start_index : end_index + 1]
        return sum(1 for message in messages if message.role == MessageRole.ASSISTANT)
