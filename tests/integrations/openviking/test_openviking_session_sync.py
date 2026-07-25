import unittest
from datetime import datetime, timedelta, timezone

from pickel.conversations.agent_message import AssistantMessage, UserMessage
from pickel.conversations.content_blocks import TextContent
from pickel.conversations.session import Session
from pickel.integrations.openviking.commit_policy import ThresholdCommitPolicy
from pickel.integrations.openviking.config import OpenVikingConfig
from pickel.integrations.openviking.openviking_state import InMemoryOpenVikingStateStore
from pickel.integrations.openviking.session_client import SyncHTTPOpenVikingSessionClient
from pickel.integrations.openviking.session_message_mapper import SessionMessageMapper
from pickel.integrations.openviking.session_messages import (
    agent_message_plain_text,
    list_syncable_agent_messages,
)
from pickel.integrations.openviking.session_sync import OpenVikingSessionSync


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
    def setUp(self) -> None:
        self.state_store = InMemoryOpenVikingStateStore()

    def _config(self) -> OpenVikingConfig:
        return OpenVikingConfig(
            enabled=True,
            base_url="https://openviking.example",
            account_id="pickel",
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
            state_store=self.state_store,
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
        session.append_user(UserMessage(content=[TextContent(text="hello")]))
        session.append_assistant(AssistantMessage(content=[TextContent(text="hi")]))

        self._sync(client).sync_pending_messages(session=session)

        state = self.state_store.get_or_create(session.session_id)
        self.assertEqual(2, len(client.appended))
        self.assertEqual("user", client.appended[0]["role"])
        self.assertEqual("assistant", client.appended[1]["role"])
        self.assertEqual("session-1", state.remote_session_id)
        self.assertEqual(1, state.last_synced_message_index)
        self.assertEqual("pickel", state.openviking_account_id)
        self.assertEqual("ssunxie", state.openviking_user_id)
        self.assertEqual("remote-pickle", state.openviking_agent_id)

    def test_failed_sync_preserves_watermark_for_retry(self) -> None:
        client = FailingAppendClient()
        session = Session.create(agent_id="Pickle", session_id="session-1")
        session.append_user(UserMessage(content=[TextContent(text="hello")]))

        self._sync(client).sync_pending_messages(session=session)

        state = self.state_store.get_or_create(session.session_id)
        self.assertIsNone(state.last_synced_message_index)
        pending = list_syncable_agent_messages(session)[state.pending_sync_start_index() :]
        self.assertEqual(["hello"], [agent_message_plain_text(m) for m in pending])

    def test_partial_sync_advances_watermark_for_successful_messages(self) -> None:
        client = FailingSecondAppendClient()
        session = Session.create(agent_id="Pickle", session_id="session-1")
        session.append_user(UserMessage(content=[TextContent(text="hello")]))
        session.append_assistant(AssistantMessage(content=[TextContent(text="hi")]))

        self._sync(client).sync_pending_messages(session=session)

        state = self.state_store.get_or_create(session.session_id)
        self.assertEqual(1, len(client.appended))
        self.assertEqual(0, state.last_synced_message_index)
        pending = list_syncable_agent_messages(session)[state.pending_sync_start_index() :]
        self.assertEqual(
            ["hi"],
            [agent_message_plain_text(m) for m in pending],
        )

    def test_force_commit_advances_commit_watermark(self) -> None:
        client = FakeSDKClient()
        session = Session.create(agent_id="Pickle", session_id="session-1")
        session.append_user(UserMessage(content=[TextContent(text="hello")]))
        sync = self._sync(client, now=datetime(2026, 4, 13, 2, tzinfo=timezone.utc))
        state = sync.state_for(session)
        state.mark_messages_synced(remote_session_id="session-1", last_message_index=0)
        sync.save_state(session, state)

        sync.commit_pending_messages(session=session, force=True)

        state = sync.state_for(session)
        self.assertEqual(["session-1"], client.committed)
        self.assertEqual(0, state.last_committed_message_index)
        self.assertEqual(datetime(2026, 4, 13, 2, tzinfo=timezone.utc), state.last_committed_at)

    def test_policy_driven_commit_runs_after_sync(self) -> None:
        client = FakeSDKClient()
        session = Session.create(agent_id="Pickle", session_id="session-1")
        session.append_assistant(AssistantMessage(content=[TextContent(text="one")]))

        self._sync(client, commit_after_turns=1).sync_pending_messages(session=session)

        state = self.state_store.get_or_create(session.session_id)
        self.assertEqual(["session-1"], client.committed)
        self.assertEqual(0, state.last_committed_message_index)

    def test_delete_session_removes_remote_session(self) -> None:
        client = FakeSDKClient()
        client.existing.add("session-1")
        session = Session.create(agent_id="Pickle", session_id="session-1")
        sync = self._sync(client)
        state = sync.state_for(session)
        state.remote_session_id = "session-1"
        sync.save_state(session, state)

        sync.delete_session(session=session)

        self.assertEqual(["session-1"], client.deleted)

    def test_delete_session_treats_missing_remote_as_success(self) -> None:
        client = FakeSDKClient()
        session = Session.create(agent_id="Pickle", session_id="session-1")

        self._sync(client).delete_session(session=session)

        self.assertEqual([], client.deleted)


if __name__ == "__main__":
    unittest.main()
