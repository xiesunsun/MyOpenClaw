"""OpenViking 旁路状态存储（不污染 Session 核心字段）。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from pickel.extensions.openviking.openviking_state import OpenVikingSessionState


class OpenVikingBypassStore:
    """独立表 integration_openviking_sessions。"""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._db_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _ensure_schema(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS integration_openviking_sessions (
                    session_id TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL
                )
                """)

    def get(self, session_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM integration_openviking_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        if row is None:
            return None
        return json.loads(str(row["payload_json"]))

    def put(self, session_id: str, payload: dict[str, Any]) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO integration_openviking_sessions(session_id, payload_json)
                VALUES (?, ?)
                ON CONFLICT(session_id) DO UPDATE SET payload_json = excluded.payload_json
                """,
                (session_id, json.dumps(payload)),
            )

    def get_state(self, session_id: str) -> OpenVikingSessionState | None:
        raw = self.get(session_id)
        if raw is None:
            return None
        return OpenVikingSessionState.from_dict(raw)

    def put_state(self, session_id: str, state: OpenVikingSessionState) -> None:
        self.put(session_id, state.to_dict())

    def get_or_create(self, session_id: str) -> OpenVikingSessionState:
        state = self.get_state(session_id)
        if state is None:
            state = OpenVikingSessionState()
            self.put_state(session_id, state)
        return state
