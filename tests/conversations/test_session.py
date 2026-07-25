"""Session entry API 单测（替代原线性 messages / OpenViking 同步字段）。"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from pickel.conversations.agent_message import (
    AssistantMessage,
    ModelResponseMetadata,
    ModelUsage,
    ToolResultMessage,
    UserMessage,
    agent_message_from_dict,
)
from pickel.conversations.content_blocks import (
    TextContent,
    ThinkingContent,
    ToolCallContent,
)
from pickel.conversations.session import Session
from pickel.conversations.session_entry import ENTRY_TYPE_MESSAGE


class SessionTests(unittest.TestCase):
    def test_session_create_binds_session_to_agent(self) -> None:
        session = Session.create(
            agent_id="Pickle",
            cwd="/proj-a",
            session_id="session-1",
        )

        self.assertEqual("session-1", session.session_id)
        self.assertEqual("Pickle", session.agent_id)
        self.assertEqual("/proj-a", session.cwd)
        self.assertEqual([], session.entries)
        self.assertIsNone(session.leaf_id)

    def test_session_create_populates_persistence_metadata(self) -> None:
        session = Session.create(
            agent_id="Pickle",
            cwd="/proj-a",
            session_id="session-1",
        )

        self.assertEqual("active", session.status)
        self.assertEqual("/proj-a", session.cwd)
        self.assertIsNotNone(session.created_at)
        self.assertIsNotNone(session.updated_at)
        self.assertEqual(timezone.utc, session.created_at.tzinfo)
        self.assertEqual(session.created_at, session.updated_at)
        self.assertIsNone(session.title)
        self.assertIsNone(session.leaf_id)

    def test_session_stores_model_visible_messages_as_entries(self) -> None:
        session = Session(session_id="session-1", agent_id="Pickle")

        user_entry = session.append_user(
            UserMessage(content=[TextContent(text="hello")])
        )
        metadata = ModelResponseMetadata(
            provider="google/gemini",
            model="gemini-3-flash-preview",
            usage=ModelUsage(input_tokens=12, output_tokens=8),
            elapsed_ms=34,
        )
        assistant_entry = session.append_assistant(
            AssistantMessage(
                content=[TextContent(text="hi there")],
                metadata=metadata,
            )
        )

        self.assertEqual("Pickle", session.agent_id)
        self.assertEqual(
            [user_entry.entry_id, assistant_entry.entry_id],
            [e.entry_id for e in session.active_path()],
        )
        restored_user = agent_message_from_dict(user_entry.payload)
        restored_assistant = agent_message_from_dict(assistant_entry.payload)
        self.assertEqual(UserMessage(content=[TextContent(text="hello")]), restored_user)
        self.assertEqual(
            AssistantMessage(
                content=[TextContent(text="hi there")],
                metadata=metadata,
            ),
            restored_assistant,
        )

    def test_session_can_store_assistant_tool_calls(self) -> None:
        session = Session(session_id="session-1", agent_id="Pickle")

        assistant_entry = session.append_assistant(
            AssistantMessage(
                content=[
                    ToolCallContent(
                        id="call-1",
                        name="echo",
                        arguments={"text": "ping"},
                    )
                ]
            )
        )
        tool_entry = session.append_tool_result(
            ToolResultMessage(
                tool_call_id="call-1",
                tool_name="echo",
                content=[TextContent(text="ping")],
            )
        )

        self.assertEqual(ENTRY_TYPE_MESSAGE, assistant_entry.entry_type)
        restored = agent_message_from_dict(assistant_entry.payload)
        assert isinstance(restored, AssistantMessage)
        self.assertEqual("echo", restored.content[0].name)  # type: ignore[attr-defined]
        self.assertEqual("call-1", restored.content[0].id)  # type: ignore[attr-defined]

        restored_tool = agent_message_from_dict(tool_entry.payload)
        assert isinstance(restored_tool, ToolResultMessage)
        self.assertEqual("call-1", restored_tool.tool_call_id)
        self.assertEqual("echo", restored_tool.tool_name)

    def test_session_can_store_thinking_blocks(self) -> None:
        session = Session(session_id="session-1", agent_id="Pickle")

        entry = session.append_assistant(
            AssistantMessage(
                content=[
                    ThinkingContent(text="intermediate", signature="sig-1"),
                    TextContent(text="hi there"),
                ]
            )
        )

        restored = agent_message_from_dict(entry.payload)
        assert isinstance(restored, AssistantMessage)
        self.assertEqual(
            [
                ThinkingContent(text="intermediate", signature="sig-1"),
                TextContent(text="hi there"),
            ],
            list(restored.content),
        )

    def test_touch_updates_updated_at(self) -> None:
        session = Session.create(agent_id="Pickle", session_id="session-1")
        touched_at = session.updated_at.replace(
            microsecond=session.updated_at.microsecond + 1
        )

        session.touch(at=touched_at)

        self.assertEqual(touched_at, session.updated_at)

    def test_append_sets_parent_to_previous_leaf(self) -> None:
        session = Session.create(agent_id="Pickle", session_id="session-1")
        first = session.append_user(UserMessage(content=[TextContent(text="one")]))
        second = session.append_user(UserMessage(content=[TextContent(text="two")]))

        self.assertIsNone(first.parent_id)
        self.assertEqual(first.entry_id, second.parent_id)
        self.assertEqual(second.entry_id, session.leaf_id)


if __name__ == "__main__":
    unittest.main()
