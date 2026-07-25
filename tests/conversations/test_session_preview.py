import unittest
from datetime import datetime, timezone

from pickel.conversations.agent_message import (
    AssistantMessage,
    ToolResultMessage,
    UserMessage,
    agent_message_to_dict,
)
from pickel.conversations.content_blocks import TextContent, ToolCallContent
from pickel.conversations.session_preview import (
    SessionPreview,
    preview_text_from_message_payload,
)


class SessionPreviewTests(unittest.TestCase):
    def test_last_message_uses_content_and_truncates(self) -> None:
        preview = SessionPreview(
            session_id="session-1",
            agent_id="Pickle",
            created_at=datetime(2026, 4, 13, tzinfo=timezone.utc),
            updated_at=datetime(2026, 4, 13, 1, tzinfo=timezone.utc),
            status="active",
            message_count=3,
            last_message="x" * 60,
        )

        self.assertEqual(("x" * 50) + "...", preview.last_message)

    def test_last_message_normalizes_whitespace(self) -> None:
        preview = SessionPreview(
            session_id="session-1",
            agent_id="Pickle",
            created_at=datetime(2026, 4, 13, tzinfo=timezone.utc),
            updated_at=datetime(2026, 4, 13, 1, tzinfo=timezone.utc),
            status="active",
            message_count=1,
            last_message="hello   \n   world",
        )

        self.assertEqual("hello world", preview.last_message)

    def test_last_message_can_hold_tool_preview(self) -> None:
        preview = SessionPreview(
            session_id="session-1",
            agent_id="Pickle",
            created_at=datetime(2026, 4, 13, tzinfo=timezone.utc),
            updated_at=datetime(2026, 4, 13, 1, tzinfo=timezone.utc),
            status="active",
            message_count=3,
            last_message="[tools] read_file, grep_search",
        )

        self.assertEqual("[tools] read_file, grep_search", preview.last_message)

    def test_preview_from_assistant_tool_calls_payload(self) -> None:
        payload = agent_message_to_dict(
            AssistantMessage(
                content=[
                    ToolCallContent(id="c1", name="read_file", arguments={}),
                    ToolCallContent(id="c2", name="grep_search", arguments={}),
                ]
            )
        )
        self.assertEqual(
            "[tools] read_file, grep_search",
            preview_text_from_message_payload(payload),
        )

    def test_preview_from_tool_result_uses_text(self) -> None:
        payload = agent_message_to_dict(
            ToolResultMessage(
                tool_call_id="c1",
                tool_name="read_file",
                content=[TextContent(text="  file body\nline2  ")],
            )
        )
        self.assertEqual("file body line2", preview_text_from_message_payload(payload))

    def test_preview_prefers_text_over_tool_names(self) -> None:
        payload = agent_message_to_dict(
            AssistantMessage(
                content=[
                    TextContent(text="working"),
                    ToolCallContent(id="c1", name="read_file", arguments={}),
                ]
            )
        )
        self.assertEqual("working", preview_text_from_message_payload(payload))

    def test_preview_from_user_text(self) -> None:
        payload = agent_message_to_dict(
            UserMessage(content=[TextContent(text="hello")])
        )
        self.assertEqual("hello", preview_text_from_message_payload(payload))


if __name__ == "__main__":
    unittest.main()
