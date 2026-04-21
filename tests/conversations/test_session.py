import unittest
from datetime import datetime, timezone

from myopenclaw.conversations.message import (
    MessageRole,
    SessionMessage,
    ToolCall,
    ToolCallBatch,
    ToolCallResult,
)
from myopenclaw.conversations.metadata import MessageMetadata
from myopenclaw.conversations.session import Session


class SessionTests(unittest.TestCase):
    def test_session_create_binds_session_to_agent(self) -> None:
        session = Session.create(agent_id="Pickle", session_id="session-1")

        self.assertEqual("session-1", session.session_id)
        self.assertEqual("Pickle", session.agent_id)
        self.assertEqual([], session.messages)

    def test_session_create_populates_persistence_metadata(self) -> None:
        session = Session.create(agent_id="Pickle", session_id="session-1")

        self.assertEqual("active", session.status)
        self.assertIsNotNone(session.created_at)
        self.assertIsNotNone(session.updated_at)
        self.assertEqual(timezone.utc, session.created_at.tzinfo)
        self.assertEqual(session.created_at, session.updated_at)
        self.assertIsNone(session.remote_session_id)
        self.assertIsNone(session.last_synced_message_index)
        self.assertIsNone(session.last_committed_message_index)
        self.assertIsNone(session.last_committed_at)
        self.assertIsNone(session.openviking_account_id)
        self.assertIsNone(session.openviking_user_id)
        self.assertIsNone(session.openviking_agent_id)

    def test_session_belongs_to_one_agent_and_stores_model_visible_messages(self) -> None:
        session = Session(session_id="session-1", agent_id="Pickle")

        session.append_user_message("hello")
        metadata = MessageMetadata(
            provider="google/gemini",
            model="gemini-3-flash-preview",
            input_tokens=12,
            output_tokens=8,
            elapsed_ms=34,
        )
        session.append_assistant_message("hi there", metadata=metadata)

        self.assertEqual("Pickle", session.agent_id)
        self.assertEqual(
            [
                SessionMessage(role=MessageRole.USER, content="hello"),
                SessionMessage(
                    role=MessageRole.ASSISTANT,
                    content="hi there",
                    metadata=metadata,
                ),
            ],
            session.messages,
        )

    def test_session_can_store_assistant_tool_batch(self) -> None:
        session = Session(session_id="session-1", agent_id="Pickle")

        batch = ToolCallBatch(
            batch_id="batch-1",
            step_index=1,
            calls=[
                ToolCall(
                    id="call-1",
                    name="echo",
                    arguments={"text": "ping"},
                )
            ],
            results=[
                ToolCallResult(
                    call_id="call-1",
                    content="ping",
                    metadata={"exit_code": 0},
                )
            ],
        )
        session.append_assistant_tool_batch(batch)

        self.assertEqual(MessageRole.ASSISTANT, session.messages[0].role)
        self.assertEqual("echo", session.messages[0].tool_call_batch.calls[0].name)
        self.assertEqual("call-1", session.messages[0].tool_call_batch.results[0].call_id)
        self.assertEqual({"exit_code": 0}, session.messages[0].tool_call_batch.results[0].metadata)

    def test_session_can_store_provider_thinking_blocks(self) -> None:
        session = Session(session_id="session-1", agent_id="Pickle")

        session.append_assistant_message(
            "hi there",
            provider_thinking_blocks=[
                {"type": "thinking", "thinking": "intermediate", "signature": "sig-1"}
            ],
        )

        self.assertEqual(
            [{"type": "thinking", "thinking": "intermediate", "signature": "sig-1"}],
            session.messages[0].provider_thinking_blocks,
        )

    def test_touch_updates_updated_at(self) -> None:
        session = Session.create(agent_id="Pickle", session_id="session-1")
        touched_at = session.updated_at.replace(microsecond=session.updated_at.microsecond + 1)

        session.touch(at=touched_at)

        self.assertEqual(touched_at, session.updated_at)

    def test_bind_openviking_records_remote_identity(self) -> None:
        session = Session.create(agent_id="Pickle", session_id="session-1")

        session.bind_openviking(
            account_id="myopenclaw",
            user_id="ssunxie",
            agent_id="remote-pickle",
        )

        self.assertEqual("myopenclaw", session.openviking_account_id)
        self.assertEqual("ssunxie", session.openviking_user_id)
        self.assertEqual("remote-pickle", session.openviking_agent_id)

    def test_pending_sync_messages_use_sync_watermark(self) -> None:
        session = Session.create(agent_id="Pickle", session_id="session-1")
        session.messages = [
            SessionMessage(role=MessageRole.USER, content="one"),
            SessionMessage(role=MessageRole.ASSISTANT, content="two"),
            SessionMessage(role=MessageRole.USER, content="three"),
        ]

        self.assertEqual(0, session.pending_sync_start_index())
        self.assertEqual(["one", "two", "three"], [m.content for m in session.pending_sync_messages()])

        session.mark_messages_synced(remote_session_id="session-1", last_message_index=1)

        self.assertEqual(2, session.pending_sync_start_index())
        self.assertEqual(["three"], [m.content for m in session.pending_sync_messages()])

    def test_commit_watermark_tracks_pending_remote_commit(self) -> None:
        session = Session.create(agent_id="Pickle", session_id="session-1")

        self.assertFalse(session.has_pending_remote_commit())

        session.mark_messages_synced(remote_session_id="session-1", last_message_index=2)

        self.assertTrue(session.has_pending_remote_commit())

        committed_at = datetime(2026, 4, 13, tzinfo=timezone.utc)
        session.mark_messages_committed(last_message_index=2, committed_at=committed_at)

        self.assertFalse(session.has_pending_remote_commit())
        self.assertEqual(2, session.last_committed_message_index)
        self.assertEqual(committed_at, session.last_committed_at)

    def test_commit_watermark_cannot_exceed_sync_watermark(self) -> None:
        session = Session.create(agent_id="Pickle", session_id="session-1")
        session.mark_messages_synced(remote_session_id="session-1", last_message_index=1)

        with self.assertRaises(ValueError):
            session.mark_messages_committed(
                last_message_index=2,
                committed_at=datetime(2026, 4, 13, tzinfo=timezone.utc),
            )


if __name__ == "__main__":
    unittest.main()
