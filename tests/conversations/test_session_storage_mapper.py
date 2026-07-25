"""session_storage_mapper：entry payload（AgentMessage dict）往返。"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from myopenclaw.conversations.agent_message import (
    AssistantMessage,
    ModelResponseMetadata,
    ModelUsage,
    ToolResultMessage,
    UserMessage,
    agent_message_from_dict,
    agent_message_to_dict,
)
from myopenclaw.conversations.content_blocks import TextContent, ToolCallContent
from myopenclaw.conversations.session import Session
from myopenclaw.conversations.session_entry import ENTRY_TYPE_MESSAGE, SessionEntry
from myopenclaw.conversations.session_storage_mapper import (
    build_session_preview,
    session_entry_from_record,
    session_entry_to_record,
    session_from_storage,
    session_to_metadata_record,
)


class SessionStorageMapperTests(unittest.TestCase):
    def test_session_entry_round_trips_agent_message_payload(self) -> None:
        message = AssistantMessage(
            content=[
                ToolCallContent(
                    id="call-1",
                    name="read_file",
                    arguments={"path": "README.md"},
                    thought_signature="sig",
                )
            ],
            metadata=ModelResponseMetadata(
                provider="google/gemini",
                model="gemini-3-flash-preview",
                usage=ModelUsage(input_tokens=12),
            ),
        )
        entry = SessionEntry(
            entry_id="entry-1",
            session_id="session-1",
            parent_id=None,
            entry_type=ENTRY_TYPE_MESSAGE,
            payload=agent_message_to_dict(message),
            created_at=datetime(2026, 4, 13, tzinfo=timezone.utc),
        )

        record = session_entry_to_record(entry)
        restored = session_entry_from_record(record)

        self.assertEqual(entry.entry_id, restored.entry_id)
        self.assertEqual(entry.session_id, restored.session_id)
        self.assertIsNone(restored.parent_id)
        self.assertEqual(ENTRY_TYPE_MESSAGE, restored.entry_type)
        self.assertEqual(message, agent_message_from_dict(restored.payload))

    def test_session_round_trips_through_storage_records(self) -> None:
        created_at = datetime(2026, 4, 13, tzinfo=timezone.utc)
        updated_at = datetime(2026, 4, 13, 1, tzinfo=timezone.utc)
        user_payload = agent_message_to_dict(
            UserMessage(content=[TextContent(text="hello")])
        )
        entry = SessionEntry(
            entry_id="entry-1",
            session_id="session-1",
            parent_id=None,
            entry_type=ENTRY_TYPE_MESSAGE,
            payload=user_payload,
            created_at=updated_at,
        )
        session = Session(
            session_id="session-1",
            agent_id="Pickle",
            cwd="/proj-a",
            leaf_id=entry.entry_id,
            entries=[entry],
            created_at=created_at,
            updated_at=updated_at,
            status="active",
            title="hello chat",
        )

        restored = session_from_storage(
            session_record=session_to_metadata_record(session),
            entry_records=[session_entry_to_record(entry)],
        )

        self.assertEqual("Pickle", restored.agent_id)
        self.assertEqual("/proj-a", restored.cwd)
        self.assertEqual(entry.entry_id, restored.leaf_id)
        self.assertEqual("hello chat", restored.title)
        self.assertEqual(1, len(restored.entries))
        self.assertEqual("hello", restored.entries[0].payload["content"][0]["text"])
        self.assertEqual(1, len(restored.active_path()))

    def test_session_preview_last_message_prefers_tool_names_when_content_is_empty(
        self,
    ) -> None:
        session = Session(session_id="session-1", agent_id="Pickle", cwd="/proj-a")
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

        preview = build_session_preview(session=session)

        self.assertEqual("[tools] read_file", preview.last_message)
        self.assertEqual(1, preview.message_count)
        self.assertEqual("/proj-a", preview.cwd)

    def test_session_preview_tool_result_uses_truncated_text(self) -> None:
        session = Session(session_id="session-1", agent_id="Pickle")
        long_body = "result " * 20
        session.append_tool_result(
            ToolResultMessage(
                tool_call_id="call-1",
                tool_name="read_file",
                content=[TextContent(text=long_body)],
            )
        )

        preview = build_session_preview(session=session)

        self.assertTrue(preview.last_message.endswith("..."))
        self.assertLessEqual(len(preview.last_message), 53)
        self.assertTrue(preview.last_message.startswith("result"))

    def test_session_preview_message_count_is_active_path_messages_only(self) -> None:
        session = Session(session_id="session-1", agent_id="Pickle")
        session.append_user(UserMessage(content=[TextContent(text="one")]))
        branch_root = session.leaf_id
        assert branch_root is not None
        session.append_user(UserMessage(content=[TextContent(text="two")]))
        session.move_leaf(branch_root)
        session.append_compaction(
            {
                "summary": "ignored for count",
                "first_kept_entry_id": branch_root,
            }
        )
        session.append_user(UserMessage(content=[TextContent(text="three")]))

        preview = build_session_preview(session=session)

        # active_path: user one → compaction → user three → 仅 2 条 message
        self.assertEqual(2, preview.message_count)
        self.assertEqual("three", preview.last_message)


if __name__ == "__main__":
    unittest.main()
