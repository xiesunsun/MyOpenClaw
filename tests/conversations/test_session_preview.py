from datetime import datetime, timezone

from pickel.conversations.agent_message import (
    AssistantMessage,
    ToolResultMessage,
    UserMessage,
)
from pickel.conversations.content_blocks import TextBlock, ToolCallBlock
from pickel.conversations.session_preview import (
    SessionPreview,
    preview_text_from_message,
)


def test_last_message_truncates_and_normalizes_whitespace() -> None:
    preview = SessionPreview(
        session_id="session-1",
        agent_id="Pickle",
        created_at=datetime(2026, 4, 13, tzinfo=timezone.utc),
        updated_at=datetime(2026, 4, 13, 1, tzinfo=timezone.utc),
        status="idle",
        message_count=1,
        last_message="x" * 60,
    )
    assert preview.last_message == ("x" * 50) + "..."


def test_preview_from_assistant_tool_calls() -> None:
    message = AssistantMessage(
        content=[
            ToolCallBlock(id="c1", name="read_file", arguments={}),
            ToolCallBlock(id="c2", name="grep_search", arguments={}),
        ]
    )
    assert preview_text_from_message(message) == "[tools] read_file, grep_search"


def test_preview_prefers_text_over_tool_names() -> None:
    message = AssistantMessage(
        content=[
            TextBlock(text="working"),
            ToolCallBlock(id="c1", name="read_file", arguments={}),
        ]
    )
    assert preview_text_from_message(message) == "working"


def test_preview_from_user_and_tool_result_text() -> None:
    assert (
        preview_text_from_message(UserMessage(content=[TextBlock(text="hello")]))
        == "hello"
    )
    assert (
        preview_text_from_message(
            ToolResultMessage(
                tool_call_id="c1",
                tool_name="read_file",
                content=[TextBlock(text="  file body\nline2  ")],
            )
        )
        == "file body line2"
    )
