import sqlite3
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
            session.last_committed_at = datetime(2026, 4, 13, 2, tzinfo=timezone.utc)
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
            self.assertEqual(
                datetime(2026, 4, 13, 2, tzinfo=timezone.utc),
                loaded.last_committed_at,
            )

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


if __name__ == "__main__":
    unittest.main()
