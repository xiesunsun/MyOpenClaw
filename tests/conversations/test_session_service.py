from __future__ import annotations

import unittest
from datetime import datetime, timezone

from myopenclaw.conversations.agent_message import AssistantMessage, UserMessage
from myopenclaw.conversations.content_blocks import TextContent, ToolCallContent
from myopenclaw.conversations.service import SessionNotFoundError, SessionService
from myopenclaw.conversations.session import Session
from myopenclaw.conversations.session_entry import SessionEntry
from myopenclaw.conversations.session_preview import SessionPreview


class FakeSessionRepository:
    def __init__(self) -> None:
        self.loaded: dict[str, Session] = {}
        self.created_sessions: list[Session] = []
        self.append_calls: list[tuple[str, int, str | None]] = []
        self.updated_metadata: list[Session] = []
        self.closed_calls: list[tuple[str, datetime]] = []
        self.deleted_session_ids: list[str] = []
        self.previews: list[SessionPreview] = []

    def create(self, session: Session) -> None:
        self.created_sessions.append(session)
        self.loaded[session.session_id] = session

    def load(self, session_id: str) -> Session | None:
        return self.loaded.get(session_id)

    def list(self, *, limit: int = 20) -> list[SessionPreview]:
        return self.previews[:limit]

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
        session = self.service.start(agent_id="Pickle")

        self.assertEqual("active", session.status)
        self.assertEqual(self.now, session.created_at)
        self.assertEqual(session, self.fake_repo.loaded["session-id"])
        self.assertEqual([], session.entries)
        self.assertIsNone(session.leaf_id)

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

    def test_list_sessions_delegates_to_repository(self) -> None:
        self.fake_repo.previews = [
            SessionPreview(
                session_id="session-1",
                agent_id="Pickle",
                created_at=self.now,
                updated_at=self.now,
                status="active",
                message_count=0,
                last_message="",
            )
        ]

        previews = self.service.list_sessions(limit=20)

        self.assertEqual(["session-1"], [preview.session_id for preview in previews])

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
