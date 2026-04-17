import unittest
from datetime import datetime, timezone

from myopenclaw.conversations.session_preview import SessionPreview


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


if __name__ == "__main__":
    unittest.main()
