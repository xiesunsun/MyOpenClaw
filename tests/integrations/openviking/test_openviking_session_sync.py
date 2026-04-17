import unittest
from datetime import datetime, timedelta, timezone

from myopenclaw.conversations.session import Session
from myopenclaw.integrations.openviking.commit_policy import ThresholdCommitPolicy
from myopenclaw.integrations.openviking.config import OpenVikingConfig
from myopenclaw.integrations.openviking.session_client import SyncHTTPOpenVikingSessionClient
from myopenclaw.integrations.openviking.session_message_mapper import SessionMessageMapper
from myopenclaw.integrations.openviking.session_sync import OpenVikingSessionSync


class NotFoundError(Exception):
    status_code = 404


class FakeSDKClient:
    def __init__(self) -> None:
        self.created: list[str] = []
        self.appended: list[dict] = []
        self.committed: list[str] = []
        self.deleted: list[str] = []
        self.existing: set[str] = set()
        self.initialize_calls = 0

    def initialize(self) -> None:
        self.initialize_calls += 1

    def get_session(self, *, session_id: str) -> dict:
        if session_id not in self.existing:
            raise NotFoundError()
        return {"session_id": session_id}

    def create_session(self, *, session_id: str) -> dict:
        self.created.append(session_id)
        self.existing.add(session_id)
        return {"session_id": session_id}

    def append_message(self, **kwargs) -> None:
        self.appended.append(kwargs)

    def commit_session(self, *, session_id: str) -> None:
        self.committed.append(session_id)

    def delete_session(self, *, session_id: str) -> None:
        if session_id not in self.existing:
            raise NotFoundError()
        self.deleted.append(session_id)
        self.existing.remove(session_id)


class FailingAppendClient(FakeSDKClient):
    def append_message(self, **kwargs) -> None:
        raise RuntimeError("remote unavailable")


class FailingSecondAppendClient(FakeSDKClient):
    def append_message(self, **kwargs) -> None:
        if self.appended:
            raise RuntimeError("remote unavailable")
        super().append_message(**kwargs)


class OpenVikingSessionSyncTests(unittest.TestCase):
    def _config(self) -> OpenVikingConfig:
        return OpenVikingConfig(
            enabled=True,
            base_url="https://openviking.example",
            account_id="myopenclaw",
            user_id="ssunxie",
            user_key="secret",
            commit_after_turns=8,
        )

    def _sync(
        self,
        client: FakeSDKClient,
        *,
        commit_after_turns: int = 8,
        now: datetime | None = None,
    ) -> OpenVikingSessionSync:
        return OpenVikingSessionSync(
            config=self._config(),
            remote_agent_id="remote-pickle",
            client=SyncHTTPOpenVikingSessionClient(self._config(), client=client),
            message_mapper=SessionMessageMapper(),
            commit_policy=ThresholdCommitPolicy(
                commit_after=timedelta(minutes=30),
                commit_after_turns=commit_after_turns,
            ),
            now=lambda: now or datetime(2026, 4, 13, tzinfo=timezone.utc),
        )

    def test_client_adapter_creates_session_when_missing(self) -> None:
        sdk_client = FakeSDKClient()
        client = SyncHTTPOpenVikingSessionClient(self._config(), client=sdk_client)

        remote_session_id = client.ensure_session(session_id="session-1")

        self.assertEqual("session-1", remote_session_id)
        self.assertEqual(["session-1"], sdk_client.created)
        self.assertEqual(1, sdk_client.initialize_calls)

    def test_sync_appends_pending_messages_and_advances_watermark(self) -> None:
        client = FakeSDKClient()
        session = Session.create(agent_id="Pickle", session_id="session-1")
        session.append_user_message("hello")
        session.append_assistant_message("hi")

        self._sync(client).sync_pending_messages(session=session)

        self.assertEqual(2, len(client.appended))
        self.assertEqual("user", client.appended[0]["role"])
        self.assertEqual("assistant", client.appended[1]["role"])
        self.assertEqual("session-1", session.remote_session_id)
        self.assertEqual(1, session.last_synced_message_index)
        self.assertEqual("myopenclaw", session.openviking_account_id)
        self.assertEqual("ssunxie", session.openviking_user_id)
        self.assertEqual("remote-pickle", session.openviking_agent_id)

    def test_failed_sync_preserves_watermark_for_retry(self) -> None:
        client = FailingAppendClient()
        session = Session.create(agent_id="Pickle", session_id="session-1")
        session.append_user_message("hello")

        self._sync(client).sync_pending_messages(session=session)

        self.assertIsNone(session.last_synced_message_index)
        self.assertEqual(["hello"], [message.content for message in session.pending_sync_messages()])

    def test_partial_sync_advances_watermark_for_successful_messages(self) -> None:
        client = FailingSecondAppendClient()
        session = Session.create(agent_id="Pickle", session_id="session-1")
        session.append_user_message("hello")
        session.append_assistant_message("hi")

        self._sync(client).sync_pending_messages(session=session)

        self.assertEqual(1, len(client.appended))
        self.assertEqual(0, session.last_synced_message_index)
        self.assertEqual(
            ["hi"],
            [message.content for message in session.pending_sync_messages()],
        )

    def test_force_commit_advances_commit_watermark(self) -> None:
        client = FakeSDKClient()
        session = Session.create(agent_id="Pickle", session_id="session-1")
        session.append_user_message("hello")
        session.mark_messages_synced(remote_session_id="session-1", last_message_index=0)
        now = datetime(2026, 4, 13, 2, tzinfo=timezone.utc)

        self._sync(client, now=now).commit_pending_messages(session=session, force=True)

        self.assertEqual(["session-1"], client.committed)
        self.assertEqual(0, session.last_committed_message_index)
        self.assertEqual(now, session.last_committed_at)

    def test_policy_driven_commit_runs_after_sync(self) -> None:
        client = FakeSDKClient()
        session = Session.create(agent_id="Pickle", session_id="session-1")
        session.append_assistant_message("one")

        self._sync(client, commit_after_turns=1).sync_pending_messages(session=session)

        self.assertEqual(["session-1"], client.committed)
        self.assertEqual(0, session.last_committed_message_index)

    def test_delete_session_removes_remote_session(self) -> None:
        client = FakeSDKClient()
        client.existing.add("session-1")
        session = Session.create(agent_id="Pickle", session_id="session-1")
        session.remote_session_id = "session-1"

        self._sync(client).delete_session(session=session)

        self.assertEqual(["session-1"], client.deleted)

    def test_delete_session_treats_missing_remote_as_success(self) -> None:
        client = FakeSDKClient()
        session = Session.create(agent_id="Pickle", session_id="session-1")

        self._sync(client).delete_session(session=session)

        self.assertEqual([], client.deleted)


if __name__ == "__main__":
    unittest.main()
