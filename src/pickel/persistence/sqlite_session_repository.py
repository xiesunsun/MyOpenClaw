"""SQLite SessionRepository：sessions + session_entries（user_version=3）。

不做旧库迁移；以空库 / 新库为准。
append_entries 在同一事务内 INSERT entries 并更新 leaf_id/updated_at。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from pickel.conversations.repository import SessionRepository
from pickel.conversations.session import Session
from pickel.conversations.session_entry import SessionEntry
from pickel.conversations.session_preview import SessionPreview
from pickel.conversations.session_storage_mapper import (
    build_session_preview,
    session_entry_to_record,
    session_from_storage,
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
                    cwd,
                    leaf_id,
                    created_at,
                    updated_at,
                    status,
                    title
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record["session_id"],
                    record["agent_id"],
                    record["cwd"],
                    record["leaf_id"],
                    record["created_at"],
                    record["updated_at"],
                    record["status"],
                    record["title"],
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
                    cwd,
                    leaf_id,
                    created_at,
                    updated_at,
                    status,
                    title
                FROM sessions
                WHERE session_id = ?
                """,
                (session_id,),
            ).fetchone()
            if session_row is None:
                return None
            entry_rows = connection.execute(
                """
                SELECT
                    entry_id,
                    session_id,
                    parent_id,
                    entry_type,
                    payload_json,
                    created_at
                FROM session_entries
                WHERE session_id = ?
                ORDER BY created_at ASC, entry_id ASC
                """,
                (session_id,),
            ).fetchall()
        return session_from_storage(
            session_record=session_row,
            entry_records=entry_rows,
        )

    def list(
        self, *, limit: int = 20, cwd: str | None = None
    ) -> list[SessionPreview]:
        """按 updated_at 降序列会话预览。

        message_count / last_message 以各 session 的 active_path 为准
        （message entry 数；不含 compaction）。
        cwd 非 None 时仅返回该工作目录下的会话。
        """
        self._ensure_schema()
        with self._connect() as connection:
            if cwd is None:
                session_rows = connection.execute(
                    """
                    SELECT
                        session_id,
                        agent_id,
                        cwd,
                        leaf_id,
                        created_at,
                        updated_at,
                        status,
                        title
                    FROM sessions
                    ORDER BY updated_at DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
            else:
                session_rows = connection.execute(
                    """
                    SELECT
                        session_id,
                        agent_id,
                        cwd,
                        leaf_id,
                        created_at,
                        updated_at,
                        status,
                        title
                    FROM sessions
                    WHERE cwd = ?
                    ORDER BY updated_at DESC
                    LIMIT ?
                    """,
                    (cwd, limit),
                ).fetchall()
            if not session_rows:
                return []

            session_ids = [str(row["session_id"]) for row in session_rows]
            placeholders = ",".join("?" for _ in session_ids)
            entry_rows = connection.execute(
                f"""
                SELECT
                    entry_id,
                    session_id,
                    parent_id,
                    entry_type,
                    payload_json,
                    created_at
                FROM session_entries
                WHERE session_id IN ({placeholders})
                ORDER BY created_at ASC, entry_id ASC
                """,
                session_ids,
            ).fetchall()

        entries_by_session: dict[str, list[sqlite3.Row]] = {
            session_id: [] for session_id in session_ids
        }
        for row in entry_rows:
            entries_by_session[str(row["session_id"])].append(row)

        previews: list[SessionPreview] = []
        for session_row in session_rows:
            session = session_from_storage(
                session_record=session_row,
                entry_records=entries_by_session[str(session_row["session_id"])],
            )
            previews.append(build_session_preview(session=session))
        return previews

    def append_entries(
        self,
        *,
        session_id: str,
        entries: list[SessionEntry],
        leaf_id: str | None,
        updated_at: datetime,
    ) -> None:
        """同一事务：INSERT entries + UPDATE sessions.leaf_id/updated_at。"""
        if not entries:
            return
        self._ensure_schema()
        records = [session_entry_to_record(entry) for entry in entries]
        with self._connect() as connection:
            for record in records:
                connection.execute(
                    """
                    INSERT INTO session_entries (
                        entry_id,
                        session_id,
                        parent_id,
                        entry_type,
                        payload_json,
                        created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record["entry_id"],
                        record["session_id"],
                        record["parent_id"],
                        record["entry_type"],
                        record["payload_json"],
                        record["created_at"],
                    ),
                )
            connection.execute(
                """
                UPDATE sessions
                SET
                    leaf_id = ?,
                    updated_at = ?
                WHERE session_id = ?
                """,
                (
                    leaf_id,
                    updated_at.isoformat(),
                    session_id,
                ),
            )

    def update_metadata(self, session: Session) -> None:
        """仅更新 title / status / leaf_id / updated_at。"""
        self._ensure_schema()
        record = session_to_metadata_record(session)
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE sessions
                SET
                    leaf_id = ?,
                    updated_at = ?,
                    status = ?,
                    title = ?
                WHERE session_id = ?
                """,
                (
                    record["leaf_id"],
                    record["updated_at"],
                    record["status"],
                    record["title"],
                    record["session_id"],
                ),
            )

    def mark_closed(self, *, session_id: str, updated_at: datetime) -> None:
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
                ("archived", updated_at.isoformat(), session_id),
            )

    def delete(self, *, session_id: str) -> None:
        self._ensure_schema()
        with self._connect() as connection:
            # 依赖 FK ON DELETE CASCADE；显式开 foreign_keys
            connection.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))

    def _ensure_schema(self) -> None:
        if self._schema_initialized and self._db_path.exists():
            return
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                PRAGMA user_version = 3;

                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    agent_id TEXT NOT NULL,
                    cwd TEXT NOT NULL,
                    leaf_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    title TEXT
                );

                CREATE TABLE IF NOT EXISTS session_entries (
                    entry_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    parent_id TEXT,
                    entry_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (session_id) REFERENCES sessions(session_id)
                        ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_session_entries_session_parent
                ON session_entries(session_id, parent_id);

                CREATE INDEX IF NOT EXISTS idx_session_entries_session_created
                ON session_entries(session_id, created_at);

                CREATE INDEX IF NOT EXISTS idx_sessions_agent_updated
                ON sessions(agent_id, updated_at);

                CREATE INDEX IF NOT EXISTS idx_sessions_cwd_updated
                ON sessions(cwd, updated_at);
                """
            )
        self._schema_initialized = True

    def _connect(self) -> sqlite3.Connection:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self._db_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection
