from __future__ import annotations

import os
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from pickel.conversations.agent_message import AssistantMessage, UserMessage
from pickel.conversations.content_blocks import TextContent, ToolCallContent
from pickel.conversations.service import SessionNotFoundError, SessionService
from pickel.conversations.session import Session
from pickel.conversations.session_entry import SessionEntry
from pickel.conversations.session_preview import SessionPreview
from pickel.integrations.openviking.session_sync import NoopSessionSync
from pickel.persistence.sqlite_session_repository import SQLiteSessionRepository


class FakeSessionRepository:
    def __init__(self) -> None:
        self.loaded: dict[str, Session] = {}
        self.created_sessions: list[Session] = []
        self.append_calls: list[tuple[str, int, str | None]] = []
        self.updated_metadata: list[Session] = []
        self.closed_calls: list[tuple[str, datetime]] = []
        self.deleted_session_ids: list[str] = []
        self.previews: list[SessionPreview] = []
        self.list_calls: list[dict[str, object]] = []

    def create(self, session: Session) -> None:
        self.created_sessions.append(session)
        self.loaded[session.session_id] = session

    def load(self, session_id: str) -> Session | None:
        return self.loaded.get(session_id)

    def list(
        self, *, limit: int = 20, cwd: str | None = None
    ) -> list[SessionPreview]:
        self.list_calls.append({"limit": limit, "cwd": cwd})
        items = self.previews
        if cwd is not None:
            items = [p for p in items if p.cwd == cwd]
        return items[:limit]

    def append_entries(
        self,
        *,
        session_id: str,
        entries: list[SessionEntry],
        leaf_id: str | None,
        updated_at: datetime,
    ) -> None:
        self.append_calls.append((session_id, len(entries), leaf_id))
        existing = self.loaded.get(session_id)
        if existing is not None:
            existing.entries.extend(entries)
            existing.leaf_id = leaf_id
            existing.updated_at = updated_at

    def update_metadata(self, session: Session) -> None:
        self.updated_metadata.append(session)
        self.loaded[session.session_id] = session

    def mark_closed(self, *, session_id: str, updated_at: datetime) -> None:
        self.closed_calls.append((session_id, updated_at))
        existing = self.loaded.get(session_id)
        if existing is not None:
            existing.status = "archived"
            existing.updated_at = updated_at

    def delete(self, *, session_id: str) -> None:
        self.deleted_session_ids.append(session_id)
        self.loaded.pop(session_id, None)


class FakeSessionSync:
    """OpenViking 字段已从 Session 移除；本 Task 仅记录调用（Task 12 恢复）。"""

    def __init__(self) -> None:
        self.synced_sessions: list[str] = []
        self.commit_calls: list[bool] = []
        self.deleted_sessions: list[str] = []

    def sync_pending_messages(self, *, session: Session) -> None:
        self.synced_sessions.append(session.session_id)

    def commit_pending_messages(
        self,
        *,
        session: Session,
        force: bool = False,
    ) -> None:
        self.commit_calls.append(force)

    def delete_session(self, *, session: Session) -> None:
        self.deleted_sessions.append(session.session_id)


class SessionServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 4, 13, tzinfo=timezone.utc)
        self.fake_repo = FakeSessionRepository()
        self.fake_sync = FakeSessionSync()
        self.service = SessionService(
            self.fake_repo,
            self.fake_sync,
            session_id_factory=lambda: "session-id",
            now=lambda: self.now,
        )

    def test_start_creates_and_persists_active_session(self) -> None:
        session = self.service.start(agent_id="Pickle", cwd="/tmp/proj-a")

        self.assertEqual("active", session.status)
        self.assertEqual(self.now, session.created_at)
        self.assertEqual(session, self.fake_repo.loaded["session-id"])
        self.assertEqual([], session.entries)
        self.assertIsNone(session.leaf_id)
        self.assertEqual(str(Path("/tmp/proj-a").resolve()), session.cwd)

    def test_start_defaults_cwd_to_process_cwd(self) -> None:
        with TemporaryDirectory() as tmpdir:
            cwd = str(Path(tmpdir).resolve())
            with patch("pickel.conversations.service.Path.cwd", return_value=Path(cwd)):
                session = self.service.start(agent_id="Pickle")

        self.assertEqual(cwd, session.cwd)

    def test_resume_loads_existing_session(self) -> None:
        session = Session.create(
            agent_id="Pickle",
            session_id="session-1",
            created_at=self.now,
        )
        self.fake_repo.loaded[session.session_id] = session

        loaded = self.service.resume(session_id="session-1")

        self.assertEqual("session-1", loaded.session_id)

    def test_resume_raises_when_session_does_not_exist(self) -> None:
        with self.assertRaises(SessionNotFoundError):
            self.service.resume(session_id="missing")

    def test_list_sessions_delegates_to_repository_with_cwd_filter(self) -> None:
        cwd_a = str(Path("/tmp/proj-a").resolve())
        self.fake_repo.previews = [
            SessionPreview(
                session_id="session-1",
                agent_id="Pickle",
                created_at=self.now,
                updated_at=self.now,
                status="active",
                message_count=0,
                last_message="",
                cwd=cwd_a,
            )
        ]

        previews = self.service.list_sessions(limit=20, cwd=cwd_a)

        self.assertEqual(["session-1"], [preview.session_id for preview in previews])
        self.assertEqual(
            [{"limit": 20, "cwd": cwd_a}],
            self.fake_repo.list_calls,
        )

    def test_list_sessions_all_sessions_skips_cwd_filter(self) -> None:
        self.fake_repo.previews = [
            SessionPreview(
                session_id="session-1",
                agent_id="Pickle",
                created_at=self.now,
                updated_at=self.now,
                status="active",
                message_count=0,
                last_message="",
                cwd="/tmp/a",
            ),
            SessionPreview(
                session_id="session-2",
                agent_id="Pickle",
                created_at=self.now,
                updated_at=self.now,
                status="active",
                message_count=0,
                last_message="",
                cwd="/tmp/b",
            ),
        ]

        previews = self.service.list_sessions(all_sessions=True)

        self.assertEqual(
            ["session-1", "session-2"],
            [preview.session_id for preview in previews],
        )
        self.assertEqual(
            [{"limit": 20, "cwd": None}],
            self.fake_repo.list_calls,
        )

    def test_list_sessions_filters_two_cwds_with_sqlite(self) -> None:
        """两个 cwd 各建 session；默认 list 只见当前 cwd；all 全见。"""
        with TemporaryDirectory() as tmpdir:
            home = Path(tmpdir) / "pickel-home"
            home.mkdir()
            db_path = home / "sessions.db"
            repo = SQLiteSessionRepository(db_path)
            service = SessionService(repo, NoopSessionSync())

            cwd_a = str((Path(tmpdir) / "proj-a").resolve())
            cwd_b = str((Path(tmpdir) / "proj-b").resolve())
            Path(cwd_a).mkdir()
            Path(cwd_b).mkdir()

            session_a = service.start(agent_id="Pickle", cwd=cwd_a)
            session_b = service.start(agent_id="Pickle", cwd=cwd_b)

            only_a = service.list_sessions(cwd=cwd_a)
            only_b = service.list_sessions(cwd=cwd_b)
            all_previews = service.list_sessions(all_sessions=True)

            self.assertEqual([session_a.session_id], [p.session_id for p in only_a])
            self.assertEqual([session_b.session_id], [p.session_id for p in only_b])
            self.assertEqual(
                {session_a.session_id, session_b.session_id},
                {p.session_id for p in all_previews},
            )

            # 全局路径：PICKEL_HOME 下 sessions.db
            with patch.dict(os.environ, {"PICKEL_HOME": str(home)}):
                from pickel.config.paths import sessions_db_path

                self.assertEqual(db_path, sessions_db_path())

    def test_build_preview_uses_last_message_rules(self) -> None:
        session = Session(
            session_id="session-1",
            agent_id="Pickle",
            created_at=self.now,
            updated_at=self.now,
        )
        session.append_assistant(
            AssistantMessage(
                content=[
                    ToolCallContent(
                        id="call-1",
                        name="read_file",
                        arguments={},
                    )
                ]
            )
        )

        preview = self.service.build_preview(session=session)

        self.assertEqual("[tools] read_file", preview.last_message)
        self.assertEqual(1, preview.message_count)

    def test_flush_new_entries_appends_then_syncs_and_updates_metadata(self) -> None:
        session = Session.create(
            agent_id="Pickle",
            session_id="session-1",
            created_at=self.now,
        )
        entry = session.append_user(UserMessage(content=[TextContent(text="hello")]))
        self.fake_repo.loaded[session.session_id] = session

        self.service.flush_new_entries(session=session, entries=[entry])

        self.assertEqual(
            [("session-1", 1, entry.entry_id)],
            self.fake_repo.append_calls,
        )
        self.assertEqual(["session-1"], self.fake_sync.synced_sessions)
        self.assertEqual(session, self.fake_repo.updated_metadata[0])

    def test_close_archives_and_force_commits_then_persists_metadata(self) -> None:
        session = Session.create(
            agent_id="Pickle",
            session_id="session-1",
            created_at=self.now,
        )
        session.append_user(UserMessage(content=[TextContent(text="hello")]))

        self.service.close(session=session)

        self.assertEqual("archived", session.status)
        self.assertEqual(["session-1"], self.fake_sync.synced_sessions)
        self.assertEqual([True], self.fake_sync.commit_calls)
        self.assertEqual([("session-1", self.now)], self.fake_repo.closed_calls)
        self.assertEqual("archived", self.fake_repo.updated_metadata[-1].status)

    def test_delete_removes_remote_before_local_session(self) -> None:
        session = Session.create(
            agent_id="Pickle",
            session_id="session-1",
            created_at=self.now,
        )
        self.fake_repo.loaded[session.session_id] = session

        self.service.delete(session_id="session-1")

        self.assertEqual(["session-1"], self.fake_sync.deleted_sessions)
        self.assertEqual(["session-1"], self.fake_repo.deleted_session_ids)

    def test_delete_raises_when_session_does_not_exist(self) -> None:
        with self.assertRaises(SessionNotFoundError):
            self.service.delete(session_id="missing")


if __name__ == "__main__":
    unittest.main()
