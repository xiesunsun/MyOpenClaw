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
from myopenclaw.conversations.session_storage_mapper import (
    build_session_preview,
    session_from_storage,
    session_message_from_record,
    session_message_to_record,
    session_to_metadata_record,
)


class SessionStorageMapperTests(unittest.TestCase):
    def test_session_message_round_trips_through_storage_record(self) -> None:
        message = SessionMessage(
            role=MessageRole.ASSISTANT,
            content="",
            metadata=MessageMetadata(
                provider="google/gemini",
                model="gemini-3-flash-preview",
                input_tokens=12,
            ),
            tool_call_batch=ToolCallBatch(
                batch_id="batch-1",
                step_index=1,
                calls=[
                    ToolCall(
                        id="call-1",
                        name="read_file",
                        arguments={"path": "README.md"},
                        thought_signature=b"sig",
                    )
                ],
                results=[
                    ToolCallResult(
                        call_id="call-1",
                        content="contents",
                        metadata={"exit_code": 0},
                    )
                ],
            ),
        )

        record = session_message_to_record(
            session_id="session-1",
            message_index=0,
            message=message,
            created_at=datetime(2026, 4, 13, tzinfo=timezone.utc),
        )
        restored = session_message_from_record(record)

        self.assertEqual(message.role, restored.role)
        self.assertEqual(
            message.tool_call_batch.calls[0].name,
            restored.tool_call_batch.calls[0].name,
        )
        self.assertEqual(
            message.tool_call_batch.calls[0].thought_signature,
            restored.tool_call_batch.calls[0].thought_signature,
        )
        self.assertEqual(message.metadata.provider, restored.metadata.provider)

    def test_session_round_trips_through_storage_records(self) -> None:
        created_at = datetime(2026, 4, 13, tzinfo=timezone.utc)
        updated_at = datetime(2026, 4, 13, 1, tzinfo=timezone.utc)
        session = Session(
            session_id="session-1",
            agent_id="Pickle",
            messages=[SessionMessage(role=MessageRole.USER, content="hello")],
            created_at=created_at,
            updated_at=updated_at,
            status="active",
            remote_session_id="remote-1",
            last_synced_message_index=0,
            last_committed_message_index=0,
            last_committed_at=updated_at,
            openviking_account_id="myopenclaw",
            openviking_user_id="ssunxie",
            openviking_agent_id="remote-pickle",
        )

        restored = session_from_storage(
            session_record=session_to_metadata_record(session),
            message_records=[
                session_message_to_record(
                    session_id="session-1",
                    message_index=0,
                    message=session.messages[0],
                    created_at=updated_at,
                )
            ],
        )

        self.assertEqual("Pickle", restored.agent_id)
        self.assertEqual("hello", restored.messages[0].content)
        self.assertEqual("remote-1", restored.remote_session_id)
        self.assertEqual(0, restored.last_synced_message_index)
        self.assertEqual(0, restored.last_committed_message_index)
        self.assertEqual(updated_at, restored.last_committed_at)
        self.assertEqual("myopenclaw", restored.openviking_account_id)
        self.assertEqual("ssunxie", restored.openviking_user_id)
        self.assertEqual("remote-pickle", restored.openviking_agent_id)

    def test_session_preview_last_message_prefers_tool_names_when_content_is_empty(self) -> None:
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
        )

        preview = build_session_preview(session=session)

        self.assertEqual("[tools] read_file", preview.last_message)
        self.assertEqual(1, preview.message_count)


if __name__ == "__main__":
    unittest.main()
