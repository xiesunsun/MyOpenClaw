"""SQLiteSessionRepository：sessions + session_entries（user_version=3）。"""

from __future__ import annotations

import shutil
import sqlite3
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from pickel.conversations.agent_message import AssistantMessage, UserMessage
from pickel.conversations.content_blocks import TextContent
from pickel.conversations.session import Session
from pickel.persistence.sqlite_session_repository import SQLiteSessionRepository


class SQLiteSessionRepositoryTests(unittest.TestCase):
    def test_create_and_load_round_trip(self) -> None:
        with TemporaryDirectory() as tmpdir:
            repo = SQLiteSessionRepository(Path(tmpdir) / "sessions.db")
            created_at = datetime(2026, 4, 13, tzinfo=timezone.utc)
            session = Session.create(
                agent_id="Pickle",
                cwd="/proj-a",
                session_id="session-1",
                created_at=created_at,
            )
            session.append_user(UserMessage(content=[TextContent(text="hello")]))

            repo.create(session)
            repo.append_entries(
                session_id="session-1",
                entries=session.entries,
                leaf_id=session.leaf_id,
                updated_at=session.updated_at,
            )
            loaded = repo.load("session-1")

            self.assertIsNotNone(loaded)
            assert loaded is not None
            self.assertEqual("Pickle", loaded.agent_id)
            self.assertEqual("/proj-a", loaded.cwd)
            self.assertEqual(session.leaf_id, loaded.leaf_id)
            self.assertEqual(1, len(loaded.active_path()))
            self.assertEqual("hello", loaded.entries[0].payload["content"][0]["text"])

    def test_list_returns_session_previews_in_updated_order(self) -> None:
        with TemporaryDirectory() as tmpdir:
            repo = SQLiteSessionRepository(Path(tmpdir) / "sessions.db")
            first = Session.create(
                agent_id="Pickle",
                cwd="/proj-a",
                session_id="session-1",
                created_at=datetime(2026, 4, 13, tzinfo=timezone.utc),
            )
            second = Session.create(
                agent_id="Pickle",
                cwd="/proj-a",
                session_id="session-2",
                created_at=datetime(2026, 4, 13, 1, tzinfo=timezone.utc),
            )
            first.append_user(UserMessage(content=[TextContent(text="older")]))
            second.append_user(UserMessage(content=[TextContent(text="newer")]))
            # 保证 second 的 updated_at 更晚
            second.touch(at=first.updated_at + timedelta(minutes=1))

            repo.create(first)
            repo.create(second)
            repo.append_entries(
                session_id="session-1",
                entries=first.entries,
                leaf_id=first.leaf_id,
                updated_at=first.updated_at,
            )
            repo.append_entries(
                session_id="session-2",
                entries=second.entries,
                leaf_id=second.leaf_id,
                updated_at=second.updated_at,
            )

            previews = repo.list(limit=20)

            self.assertEqual(
                ["session-2", "session-1"],
                [preview.session_id for preview in previews],
            )
            self.assertEqual(
                ["newer", "older"],
                [preview.last_message for preview in previews],
            )
            self.assertEqual([1, 1], [preview.message_count for preview in previews])
            self.assertEqual(
                ["/proj-a", "/proj-a"],
                [preview.cwd for preview in previews],
            )

    def test_list_filters_by_cwd(self) -> None:
        with TemporaryDirectory() as tmpdir:
            repo = SQLiteSessionRepository(Path(tmpdir) / "sessions.db")
            in_a = Session.create(
                agent_id="Pickle",
                cwd="/proj-a",
                session_id="session-a",
                created_at=datetime(2026, 4, 13, tzinfo=timezone.utc),
            )
            in_b = Session.create(
                agent_id="Pickle",
                cwd="/proj-b",
                session_id="session-b",
                created_at=datetime(2026, 4, 13, 1, tzinfo=timezone.utc),
            )
            in_a.append_user(UserMessage(content=[TextContent(text="from a")]))
            in_b.append_user(UserMessage(content=[TextContent(text="from b")]))
            in_b.touch(at=in_a.updated_at + timedelta(minutes=1))

            repo.create(in_a)
            repo.create(in_b)
            repo.append_entries(
                session_id="session-a",
                entries=in_a.entries,
                leaf_id=in_a.leaf_id,
                updated_at=in_a.updated_at,
            )
            repo.append_entries(
                session_id="session-b",
                entries=in_b.entries,
                leaf_id=in_b.leaf_id,
                updated_at=in_b.updated_at,
            )

            filtered = repo.list(limit=20, cwd="/proj-a")
            all_previews = repo.list(limit=20)

            self.assertEqual(["session-a"], [p.session_id for p in filtered])
            self.assertEqual(["/proj-a"], [p.cwd for p in filtered])
            self.assertEqual(
                ["session-b", "session-a"],
                [p.session_id for p in all_previews],
            )

    def test_append_entries_only_writes_new_range(self) -> None:
        with TemporaryDirectory() as tmpdir:
            repo = SQLiteSessionRepository(Path(tmpdir) / "sessions.db")
            created_at = datetime(2026, 4, 13, tzinfo=timezone.utc)
            session = Session.create(
                agent_id="Pickle",
                cwd="/proj-a",
                session_id="session-1",
                created_at=created_at,
            )
            first = session.append_user(UserMessage(content=[TextContent(text="hello")]))
            repo.create(session)
            repo.append_entries(
                session_id="session-1",
                entries=[first],
                leaf_id=first.entry_id,
                updated_at=session.updated_at,
            )

            second = session.append_assistant(
                AssistantMessage(content=[TextContent(text="hi")])
            )
            repo.append_entries(
                session_id="session-1",
                entries=[second],
                leaf_id=second.entry_id,
                updated_at=session.updated_at,
            )
            loaded = repo.load("session-1")

            self.assertIsNotNone(loaded)
            assert loaded is not None
            self.assertEqual(2, len(loaded.entries))
            self.assertEqual(
                ["hello", "hi"],
                [
                    entry.payload["content"][0]["text"]
                    for entry in loaded.active_path()
                ],
            )
            self.assertEqual(second.entry_id, loaded.leaf_id)

    def test_update_metadata_and_mark_closed_persist_lifecycle_fields(self) -> None:
        with TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "sessions.db"
            repo = SQLiteSessionRepository(db_path)
            session = Session.create(
                agent_id="Pickle",
                cwd="/proj-a",
                session_id="session-1",
                created_at=datetime(2026, 4, 13, tzinfo=timezone.utc),
            )
            repo.create(session)
            session.title = "first chat"
            session.touch(at=datetime(2026, 4, 13, 1, tzinfo=timezone.utc))

            repo.update_metadata(session)
            repo.mark_closed(
                session_id="session-1",
                updated_at=datetime(2026, 4, 13, 3, tzinfo=timezone.utc),
            )
            loaded = repo.load("session-1")

            self.assertIsNotNone(loaded)
            assert loaded is not None
            self.assertEqual("archived", loaded.status)
            self.assertEqual("first chat", loaded.title)
            self.assertEqual("/proj-a", loaded.cwd)
            self.assertEqual(
                datetime(2026, 4, 13, 3, tzinfo=timezone.utc),
                loaded.updated_at,
            )

            with sqlite3.connect(db_path) as connection:
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type IN ('table', 'index')"
                    ).fetchall()
                }
                version = connection.execute("PRAGMA user_version").fetchone()[0]

            self.assertEqual(3, version)
            self.assertIn("sessions", tables)
            self.assertIn("session_entries", tables)
            self.assertIn("idx_sessions_agent_updated", tables)
            self.assertIn("idx_sessions_cwd_updated", tables)
            self.assertIn("idx_session_entries_session_parent", tables)

    def test_no_legacy_openviking_migration(self) -> None:
        """不做旧库迁移：全新 schema 不含 openviking 游标列。"""
        with TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "sessions.db"
            repo = SQLiteSessionRepository(db_path)
            session = Session.create(
                agent_id="Pickle",
                cwd="/proj-a",
                session_id="session-1",
                created_at=datetime(2026, 4, 13, tzinfo=timezone.utc),
            )
            repo.create(session)

            with sqlite3.connect(db_path) as connection:
                columns = {
                    row[1]
                    for row in connection.execute("PRAGMA table_info(sessions)").fetchall()
                }

            self.assertEqual(
                {
                    "session_id",
                    "agent_id",
                    "cwd",
                    "leaf_id",
                    "created_at",
                    "updated_at",
                    "status",
                    "title",
                },
                columns,
            )
            self.assertNotIn("remote_session_id", columns)
            self.assertNotIn("openviking_account_id", columns)

    def test_delete_removes_session_and_entries(self) -> None:
        with TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "sessions.db"
            repo = SQLiteSessionRepository(db_path)
            created_at = datetime(2026, 4, 13, tzinfo=timezone.utc)
            session = Session.create(
                agent_id="Pickle",
                cwd="/proj-a",
                session_id="session-1",
                created_at=created_at,
            )
            session.append_user(UserMessage(content=[TextContent(text="hello")]))
            repo.create(session)
            repo.append_entries(
                session_id="session-1",
                entries=session.entries,
                leaf_id=session.leaf_id,
                updated_at=session.updated_at,
            )

            repo.delete(session_id="session-1")

            self.assertIsNone(repo.load("session-1"))
            with sqlite3.connect(db_path) as connection:
                connection.execute("PRAGMA foreign_keys = ON")
                entry_count = connection.execute(
                    "SELECT COUNT(*) FROM session_entries WHERE session_id = ?",
                    ("session-1",),
                ).fetchone()[0]

            self.assertEqual(0, entry_count)

    def test_mark_closed_reinitializes_schema_after_database_directory_is_removed(
        self,
    ) -> None:
        with TemporaryDirectory() as tmpdir:
            db_dir = Path(tmpdir) / ".pickel"
            db_path = db_dir / "sessions.db"
            repo = SQLiteSessionRepository(db_path)
            session = Session.create(
                agent_id="Pickle",
                cwd="/proj-a",
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
            self.assertIn("session_entries", tables)


if __name__ == "__main__":
    unittest.main()
