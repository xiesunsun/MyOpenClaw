import sqlite3
import shutil
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from myopenclaw.conversations.message import MessageRole, SessionMessage
from myopenclaw.conversations.session import Session
from myopenclaw.persistence.sqlite_session_repository import SQLiteSessionRepository


class SQLiteSessionRepositoryTests(unittest.TestCase):
    def test_create_and_load_round_trip(self) -> None:
        with TemporaryDirectory() as tmpdir:
            repo = SQLiteSessionRepository(Path(tmpdir) / "sessions.db")
            created_at = datetime(2026, 4, 13, tzinfo=timezone.utc)
            session = Session.create(
                agent_id="Pickle",
                session_id="session-1",
                created_at=created_at,
            )
            session.append_user_message("hello")

            repo.create(session)
            repo.append_messages(
                session_id="session-1",
                start_index=0,
                messages=session.messages,
                updated_at=created_at,
            )
            loaded = repo.load("session-1")

            self.assertIsNotNone(loaded)
            self.assertEqual("Pickle", loaded.agent_id)
            self.assertEqual(["hello"], [message.content for message in loaded.messages])

    def test_list_returns_session_previews_in_updated_order(self) -> None:
        with TemporaryDirectory() as tmpdir:
            repo = SQLiteSessionRepository(Path(tmpdir) / "sessions.db")
            first = Session.create(
                agent_id="Pickle",
                session_id="session-1",
                created_at=datetime(2026, 4, 13, tzinfo=timezone.utc),
            )
            second = Session.create(
                agent_id="Pickle",
                session_id="session-2",
                created_at=datetime(2026, 4, 13, 1, tzinfo=timezone.utc),
            )
            first.append_user_message("older")
            second.append_user_message("newer")
            repo.create(first)
            repo.create(second)
            repo.append_messages(
                session_id="session-1",
                start_index=0,
                messages=first.messages,
                updated_at=first.updated_at,
            )
            repo.append_messages(
                session_id="session-2",
                start_index=0,
                messages=second.messages,
                updated_at=second.updated_at,
            )

            previews = repo.list(limit=20)

            self.assertEqual(["session-2", "session-1"], [preview.session_id for preview in previews])
            self.assertEqual(["newer", "older"], [preview.last_message for preview in previews])

    def test_append_messages_only_writes_new_range(self) -> None:
        with TemporaryDirectory() as tmpdir:
            repo = SQLiteSessionRepository(Path(tmpdir) / "sessions.db")
            created_at = datetime(2026, 4, 13, tzinfo=timezone.utc)
            updated_at = created_at + timedelta(minutes=1)
            session = Session.create(
                agent_id="Pickle",
                session_id="session-1",
                created_at=created_at,
            )
            session.messages = [
                SessionMessage(role=MessageRole.USER, content="hello"),
                SessionMessage(role=MessageRole.ASSISTANT, content="hi"),
            ]
            repo.create(session)
            repo.append_messages(
                session_id="session-1",
                start_index=0,
                messages=session.messages[:1],
                updated_at=created_at,
            )
            repo.append_messages(
                session_id="session-1",
                start_index=1,
                messages=session.messages[1:],
                updated_at=updated_at,
            )
            loaded = repo.load("session-1")

            self.assertEqual(2, len(loaded.messages))
            self.assertEqual(["hello", "hi"], [message.content for message in loaded.messages])

    def test_update_metadata_and_mark_closed_persist_lifecycle_fields(self) -> None:
        with TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "sessions.db"
            repo = SQLiteSessionRepository(db_path)
            session = Session.create(
                agent_id="Pickle",
                session_id="session-1",
                created_at=datetime(2026, 4, 13, tzinfo=timezone.utc),
            )
            repo.create(session)
            session.remote_session_id = "remote-1"
            session.last_synced_message_index = 2
            session.last_committed_message_index = 2
            session.last_committed_at = datetime(2026, 4, 13, 2, tzinfo=timezone.utc)
            session.openviking_account_id = "myopenclaw"
            session.openviking_user_id = "ssunxie"
            session.openviking_agent_id = "remote-pickle"
            session.touch(at=datetime(2026, 4, 13, 1, tzinfo=timezone.utc))

            repo.update_metadata(session)
            repo.mark_closed(
                session_id="session-1",
                updated_at=datetime(2026, 4, 13, 3, tzinfo=timezone.utc),
            )
            loaded = repo.load("session-1")

            self.assertEqual("closed", loaded.status)
            self.assertEqual("remote-1", loaded.remote_session_id)
            self.assertEqual(2, loaded.last_synced_message_index)
            self.assertEqual(2, loaded.last_committed_message_index)
            self.assertEqual(
                datetime(2026, 4, 13, 2, tzinfo=timezone.utc),
                loaded.last_committed_at,
            )
            self.assertEqual("myopenclaw", loaded.openviking_account_id)
            self.assertEqual("ssunxie", loaded.openviking_user_id)
            self.assertEqual("remote-pickle", loaded.openviking_agent_id)

            with sqlite3.connect(db_path) as connection:
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type IN ('table', 'index')"
                    ).fetchall()
                }

            self.assertIn("sessions", tables)
            self.assertIn("session_messages", tables)
            self.assertIn("idx_sessions_updated_at", tables)
            self.assertIn("idx_session_messages_session_id", tables)

    def test_existing_database_schema_is_migrated_with_openviking_columns(self) -> None:
        with TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "sessions.db"
            created_at = datetime(2026, 4, 13, tzinfo=timezone.utc)
            with sqlite3.connect(db_path) as connection:
                connection.executescript(
                    """
                    CREATE TABLE sessions (
                        session_id TEXT PRIMARY KEY,
                        agent_id TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        status TEXT NOT NULL,
                        remote_session_id TEXT,
                        last_synced_message_index INTEGER,
                        last_committed_at TEXT
                    );
                    CREATE TABLE session_messages (
                        session_id TEXT NOT NULL,
                        message_index INTEGER NOT NULL,
                        payload_json TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        PRIMARY KEY (session_id, message_index)
                    );
                    """
                )
                connection.execute(
                    """
                    INSERT INTO sessions (
                        session_id,
                        agent_id,
                        created_at,
                        updated_at,
                        status
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        "session-1",
                        "Pickle",
                        created_at.isoformat(),
                        created_at.isoformat(),
                        "active",
                    ),
                )

            repo = SQLiteSessionRepository(db_path)
            loaded = repo.load("session-1")

            self.assertIsNotNone(loaded)
            self.assertIsNone(loaded.last_committed_message_index)
            self.assertIsNone(loaded.openviking_account_id)

            with sqlite3.connect(db_path) as connection:
                columns = {
                    row[1]
                    for row in connection.execute("PRAGMA table_info(sessions)").fetchall()
                }

            self.assertIn("last_committed_message_index", columns)
            self.assertIn("openviking_account_id", columns)
            self.assertIn("openviking_user_id", columns)
            self.assertIn("openviking_agent_id", columns)

    def test_delete_removes_session_and_messages(self) -> None:
        with TemporaryDirectory() as tmpdir:
            repo = SQLiteSessionRepository(Path(tmpdir) / "sessions.db")
            created_at = datetime(2026, 4, 13, tzinfo=timezone.utc)
            session = Session.create(
                agent_id="Pickle",
                session_id="session-1",
                created_at=created_at,
            )
            session.append_user_message("hello")
            repo.create(session)
            repo.append_messages(
                session_id="session-1",
                start_index=0,
                messages=session.messages,
                updated_at=created_at,
            )

            repo.delete(session_id="session-1")

            self.assertIsNone(repo.load("session-1"))
            with sqlite3.connect(Path(tmpdir) / "sessions.db") as connection:
                message_count = connection.execute(
                    "SELECT COUNT(*) FROM session_messages WHERE session_id = ?",
                    ("session-1",),
                ).fetchone()[0]

            self.assertEqual(0, message_count)

    def test_mark_closed_reinitializes_schema_after_database_directory_is_removed(self) -> None:
        with TemporaryDirectory() as tmpdir:
            db_dir = Path(tmpdir) / ".myopenclaw"
            db_path = db_dir / "sessions.db"
            repo = SQLiteSessionRepository(db_path)
            session = Session.create(
                agent_id="Pickle",
                session_id="session-1",
                created_at=datetime(2026, 4, 13, tzinfo=timezone.utc),
            )
            repo.create(session)

            shutil.rmtree(db_dir)

            repo.mark_closed(
                session_id="session-1",
                updated_at=datetime(2026, 4, 13, 3, tzinfo=timezone.utc),
            )

            self.assertTrue(db_dir.exists())
            with sqlite3.connect(db_path) as connection:
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    ).fetchall()
                }

            self.assertIn("sessions", tables)
            self.assertIn("session_messages", tables)


if __name__ == "__main__":
    unittest.main()
