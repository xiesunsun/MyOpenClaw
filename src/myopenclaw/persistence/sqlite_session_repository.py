from __future__ import annotations

import sqlite3
from pathlib import Path

from myopenclaw.conversations.message import SessionMessage
from myopenclaw.conversations.repository import SessionRepository
from myopenclaw.conversations.session import Session
from myopenclaw.conversations.session_preview import SessionPreview
from myopenclaw.conversations.session_storage_mapper import (
    session_from_storage,
    session_message_to_record,
    session_preview_from_storage_record,
    session_to_metadata_record,
)


class SQLiteSessionRepository(SessionRepository):
    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._schema_initialized = False

    @property
    def db_path(self) -> Path:
        return self._db_path

    def create(self, session: Session) -> None:
        self._ensure_schema()
        record = session_to_metadata_record(session)
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO sessions (
                    session_id,
                    agent_id,
                    created_at,
                    updated_at,
                    status,
                    remote_session_id,
                    last_synced_message_index,
                    last_committed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record["session_id"],
                    record["agent_id"],
                    record["created_at"],
                    record["updated_at"],
                    record["status"],
                    record["remote_session_id"],
                    record["last_synced_message_index"],
                    record["last_committed_at"],
                ),
            )

    def load(self, session_id: str) -> Session | None:
        self._ensure_schema()
        with self._connect() as connection:
            session_row = connection.execute(
                """
                SELECT
                    session_id,
                    agent_id,
                    created_at,
                    updated_at,
                    status,
                    remote_session_id,
                    last_synced_message_index,
                    last_committed_at
                FROM sessions
                WHERE session_id = ?
                """,
                (session_id,),
            ).fetchone()
            if session_row is None:
                return None
            message_rows = connection.execute(
                """
                SELECT
                    session_id,
                    message_index,
                    payload_json,
                    created_at
                FROM session_messages
                WHERE session_id = ?
                ORDER BY message_index ASC
                """,
                (session_id,),
            ).fetchall()
        return session_from_storage(
            session_record=session_row,
            message_records=message_rows,
        )

    def list(self, *, limit: int = 20) -> list[SessionPreview]:
        self._ensure_schema()
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    s.session_id,
                    s.agent_id,
                    s.created_at,
                    s.updated_at,
                    s.status,
                    s.remote_session_id,
                    s.last_synced_message_index,
                    s.last_committed_at,
                    (
                        SELECT COUNT(*)
                        FROM session_messages sm
                        WHERE sm.session_id = s.session_id
                    ) AS message_count,
                    (
                        SELECT payload_json
                        FROM session_messages sm
                        WHERE sm.session_id = s.session_id
                        ORDER BY sm.message_index DESC
                        LIMIT 1
                    ) AS last_payload_json
                FROM sessions s
                ORDER BY s.updated_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [session_preview_from_storage_record(row) for row in rows]

    def append_messages(
        self,
        *,
        session_id: str,
        start_index: int,
        messages: list[SessionMessage],
        updated_at,
    ) -> None:
        if not messages:
            return
        self._ensure_schema()
        records = [
            session_message_to_record(
                session_id=session_id,
                message_index=start_index + offset,
                message=message,
                created_at=updated_at,
            )
            for offset, message in enumerate(messages)
        ]
        with self._connect() as connection:
            connection.executemany(
                """
                INSERT INTO session_messages (
                    session_id,
                    message_index,
                    payload_json,
                    created_at
                ) VALUES (?, ?, ?, ?)
                """,
                [
                    (
                        record["session_id"],
                        record["message_index"],
                        record["payload_json"],
                        record["created_at"],
                    )
                    for record in records
                ],
            )

    def update_metadata(self, session: Session) -> None:
        self._ensure_schema()
        record = session_to_metadata_record(session)
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE sessions
                SET
                    updated_at = ?,
                    status = ?,
                    remote_session_id = ?,
                    last_synced_message_index = ?,
                    last_committed_at = ?
                WHERE session_id = ?
                """,
                (
                    record["updated_at"],
                    record["status"],
                    record["remote_session_id"],
                    record["last_synced_message_index"],
                    record["last_committed_at"],
                    record["session_id"],
                ),
            )

    def mark_closed(self, *, session_id: str, updated_at) -> None:
        self._ensure_schema()
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE sessions
                SET
                    status = ?,
                    updated_at = ?
                WHERE session_id = ?
                """,
                ("closed", updated_at.isoformat(), session_id),
            )

    def _ensure_schema(self) -> None:
        if self._schema_initialized:
            return
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    agent_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    remote_session_id TEXT,
                    last_synced_message_index INTEGER,
                    last_committed_at TEXT
                );

                CREATE TABLE IF NOT EXISTS session_messages (
                    session_id TEXT NOT NULL,
                    message_index INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (session_id, message_index)
                );

                CREATE INDEX IF NOT EXISTS idx_sessions_updated_at
                ON sessions(updated_at DESC);

                CREATE INDEX IF NOT EXISTS idx_session_messages_session_id
                ON session_messages(session_id);
                """
            )
        self._schema_initialized = True

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._db_path)
        connection.row_factory = sqlite3.Row
        return connection
