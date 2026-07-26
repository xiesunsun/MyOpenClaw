"""OpenViking 会话旁路状态：游标与绑定信息，不进入 Session 核心。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol


@dataclass
class OpenVikingSessionState:
    """单会话的 OpenViking 同步/提交游标。"""

    remote_session_id: str | None = None
    last_synced_message_index: int | None = None
    last_committed_message_index: int | None = None
    last_committed_at: datetime | None = None
    openviking_account_id: str | None = None
    openviking_user_id: str | None = None
    openviking_agent_id: str | None = None

    def bind(self, *, account_id: str, user_id: str, agent_id: str) -> None:
        self.openviking_account_id = account_id
        self.openviking_user_id = user_id
        self.openviking_agent_id = agent_id

    def pending_sync_start_index(self) -> int:
        if self.last_synced_message_index is None:
            return 0
        return self.last_synced_message_index + 1

    def has_pending_remote_commit(self) -> bool:
        if self.last_synced_message_index is None:
            return False
        if self.last_committed_message_index is None:
            return True
        return self.last_committed_message_index < self.last_synced_message_index

    def mark_messages_synced(
        self,
        *,
        remote_session_id: str,
        last_message_index: int,
    ) -> None:
        self.remote_session_id = remote_session_id
        self.last_synced_message_index = last_message_index
        if (
            self.last_committed_message_index is not None
            and self.last_committed_message_index > last_message_index
        ):
            raise ValueError(
                "last_committed_message_index cannot exceed last_synced_message_index"
            )

    def mark_messages_committed(
        self,
        *,
        last_message_index: int,
        committed_at: datetime,
    ) -> None:
        if (
            self.last_synced_message_index is not None
            and last_message_index > self.last_synced_message_index
        ):
            raise ValueError(
                "last_committed_message_index cannot exceed last_synced_message_index"
            )
        self.last_committed_message_index = last_message_index
        self.last_committed_at = committed_at

    def to_dict(self) -> dict[str, Any]:
        return {
            "remote_session_id": self.remote_session_id,
            "last_synced_message_index": self.last_synced_message_index,
            "last_committed_message_index": self.last_committed_message_index,
            "last_committed_at": (
                self.last_committed_at.isoformat() if self.last_committed_at else None
            ),
            "openviking_account_id": self.openviking_account_id,
            "openviking_user_id": self.openviking_user_id,
            "openviking_agent_id": self.openviking_agent_id,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> "OpenVikingSessionState":
        if not payload:
            return cls()
        committed_raw = payload.get("last_committed_at")
        committed_at: datetime | None = None
        if isinstance(committed_raw, str) and committed_raw:
            committed_at = datetime.fromisoformat(committed_raw)
        return cls(
            remote_session_id=_optional_str(payload.get("remote_session_id")),
            last_synced_message_index=_optional_int(
                payload.get("last_synced_message_index")
            ),
            last_committed_message_index=_optional_int(
                payload.get("last_committed_message_index")
            ),
            last_committed_at=committed_at,
            openviking_account_id=_optional_str(payload.get("openviking_account_id")),
            openviking_user_id=_optional_str(payload.get("openviking_user_id")),
            openviking_agent_id=_optional_str(payload.get("openviking_agent_id")),
        )


class OpenVikingStateStore(Protocol):
    def get_state(self, session_id: str) -> OpenVikingSessionState | None: ...

    def put_state(self, session_id: str, state: OpenVikingSessionState) -> None: ...

    def get_or_create(self, session_id: str) -> OpenVikingSessionState: ...


class InMemoryOpenVikingStateStore:
    """测试与无 SQLite 场景的内存旁路状态。"""

    def __init__(self) -> None:
        self._states: dict[str, OpenVikingSessionState] = {}

    def get_state(self, session_id: str) -> OpenVikingSessionState | None:
        return self._states.get(session_id)

    def put_state(self, session_id: str, state: OpenVikingSessionState) -> None:
        self._states[session_id] = state

    def get_or_create(self, session_id: str) -> OpenVikingSessionState:
        state = self._states.get(session_id)
        if state is None:
            state = OpenVikingSessionState()
            self._states[session_id] = state
        return state


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)
