from __future__ import annotations

import unittest
from datetime import datetime, timezone

from myopenclaw.conversations.message import MessageRole, SessionMessage, ToolCall, ToolCallBatch
from myopenclaw.conversations.service import SessionNotFoundError, SessionService
from myopenclaw.conversations.session import Session
from myopenclaw.conversations.session_preview import SessionPreview


class FakeSessionRepository:
    def __init__(self) -> None:
        self.loaded: dict[str, Session] = {}
        self.created_sessions: list[Session] = []
        self.append_calls: list[tuple[str, int, int]] = []
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

    def append_messages(
        self,
        *,
        session_id: str,
        start_index: int,
        messages: list[SessionMessage],
        updated_at: datetime,
    ) -> None:
        self.append_calls.append((session_id, start_index, len(messages)))

    def update_metadata(self, session: Session) -> None:
        self.updated_metadata.append(session)
        self.loaded[session.session_id] = session

    def mark_closed(self, *, session_id: str, updated_at: datetime) -> None:
        self.closed_calls.append((session_id, updated_at))

    def delete(self, *, session_id: str) -> None:
        self.deleted_session_ids.append(session_id)
        self.loaded.pop(session_id, None)


class FakeSessionSync:
    def __init__(self) -> None:
        self.synced_sessions: list[str] = []
        self.commit_calls: list[bool] = []
        self.deleted_sessions: list[str] = []

    def sync_pending_messages(self, *, session: Session) -> None:
        self.synced_sessions.append(session.session_id)
        session.remote_session_id = session.session_id
        if session.messages:
            session.last_synced_message_index = len(session.messages) - 1

    def commit_pending_messages(
        self,
        *,
        session: Session,
        force: bool = False,
    ) -> None:
        self.commit_calls.append(force)
        session.last_committed_message_index = session.last_synced_message_index

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

    def test_resume_loads_existing_session(self) -> None:
        session = Session.create(agent_id="Pickle", session_id="session-1", created_at=self.now)
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
            messages=[
                SessionMessage(
                    role=MessageRole.ASSISTANT,
                    content="",
                    tool_call_batch=ToolCallBatch(
                        batch_id="batch-1",
                        step_index=1,
                        calls=[ToolCall(id="call-1", name="read_file", arguments={})],
                    ),
                )
            ],
            created_at=self.now,
            updated_at=self.now,
        )

        preview = self.service.build_preview(session=session)

        self.assertEqual("[tools] read_file", preview.last_message)

    def test_flush_new_messages_syncs_then_persists_sync_metadata(self) -> None:
        session = Session.create(agent_id="Pickle", session_id="session-1", created_at=self.now)
        session.append_user_message("hello")
        self.fake_repo.loaded[session.session_id] = session

        self.service.flush_new_messages(session=session, start_index=0)

        self.assertEqual([("session-1", 0, 1)], self.fake_repo.append_calls)
        self.assertEqual(["session-1"], self.fake_sync.synced_sessions)
        self.assertEqual(session, self.fake_repo.updated_metadata[0])
        self.assertEqual("session-1", self.fake_repo.updated_metadata[0].remote_session_id)
        self.assertEqual(0, self.fake_repo.updated_metadata[0].last_synced_message_index)

    def test_close_syncs_and_force_commits_then_persists_metadata(self) -> None:
        session = Session.create(agent_id="Pickle", session_id="session-1", created_at=self.now)
        session.append_user_message("hello")

        self.service.close(session=session)

        self.assertEqual("closed", session.status)
        self.assertEqual(["session-1"], self.fake_sync.synced_sessions)
        self.assertEqual([True], self.fake_sync.commit_calls)
        self.assertEqual([("session-1", self.now)], self.fake_repo.closed_calls)
        self.assertEqual(0, self.fake_repo.updated_metadata[-1].last_committed_message_index)

    def test_delete_removes_remote_before_local_session(self) -> None:
        session = Session.create(agent_id="Pickle", session_id="session-1", created_at=self.now)
        self.fake_repo.loaded[session.session_id] = session

        self.service.delete(session_id="session-1")

        self.assertEqual(["session-1"], self.fake_sync.deleted_sessions)
        self.assertEqual(["session-1"], self.fake_repo.deleted_session_ids)

    def test_delete_raises_when_session_does_not_exist(self) -> None:
        with self.assertRaises(SessionNotFoundError):
            self.service.delete(session_id="missing")


if __name__ == "__main__":
    unittest.main()
