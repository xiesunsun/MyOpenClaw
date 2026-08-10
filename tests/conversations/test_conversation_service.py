from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from pickel.conversations.agent_message import (
    AssistantMessage,
    ToolResultMessage,
    UserMessage,
)
from pickel.conversations.content_blocks import TextContent
from pickel.conversations.conversation_service import (
    ConversationNotFoundError,
    ConversationService,
)
from pickel.persistence.sqlite_runtime_store import SQLiteRuntimeStore


def _service(tmp_path: Path) -> ConversationService:
    return ConversationService(
        SQLiteRuntimeStore(tmp_path / "conversations.db"),
        session_id_factory=lambda: "session-1",
        now=lambda: datetime(2026, 8, 11, tzinfo=timezone.utc),
    )


def test_create_and_append_messages_through_single_transaction_path(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    session = service.create_conversation_session(
        agent_id="Pickle",
        cwd=str(tmp_path),
    )

    user_entry = service.append_user_message(
        session_id=session.session_id,
        message=UserMessage(content=[TextContent(text="hello")]),
    )
    assistant_entry = service.append_assistant_message(
        session_id=session.session_id,
        message=AssistantMessage(content=[TextContent(text="hi")]),
    )
    tool_entry = service.append_tool_result_message(
        session_id=session.session_id,
        message=ToolResultMessage(
            tool_call_id="call-1",
            tool_name="echo",
            content=[TextContent(text="done")],
        ),
    )

    entries = service.list_active_branch_entries(session_id=session.session_id)
    assert [entry.node.node_id for entry in entries] == [
        user_entry.node.node_id,
        assistant_entry.node.node_id,
        tool_entry.node.node_id,
    ]
    assert [entry.node.sequence for entry in entries] == [1, 2, 3]
    assert all(entry.object.object_type == "agent_message" for entry in entries)
    assert entries[0].object.content["role"] == "user"
    assert entries[1].object.content["role"] == "assistant"
    assert entries[2].object.content["role"] == "tool"


def test_move_active_branch_creates_reference_version_without_new_node(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    session = service.create_conversation_session(agent_id="Pickle")
    first = service.append_user_message(
        session_id=session.session_id,
        message=UserMessage(content=[TextContent(text="first")]),
    )
    service.append_assistant_message(
        session_id=session.session_id,
        message=AssistantMessage(content=[TextContent(text="discarded branch")]),
    )

    moved = service.move_active_branch_to(
        session_id=session.session_id,
        node_id=first.node.node_id,
    )
    replacement = service.append_assistant_message(
        session_id=session.session_id,
        message=AssistantMessage(content=[TextContent(text="replacement")]),
    )

    entries = service.list_active_branch_entries(session_id=session.session_id)
    assert moved.current_sequence == 3
    assert moved.active_node_id == first.node.node_id
    assert replacement.node.parent_node_id == first.node.node_id
    assert [entry.object.content["content"][0]["text"] for entry in entries] == [
        "first",
        "replacement",
    ]


def test_append_compaction_and_host_call_keep_explicit_object_types(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    session = service.create_conversation_session(agent_id="Pickle")

    service.append_history_compaction(
        session_id=session.session_id,
        content={"summary": "short"},
    )
    service.append_host_call_request(
        session_id=session.session_id,
        content={"call_id": "host-1"},
    )
    service.append_host_call_response(
        session_id=session.session_id,
        content={"call_id": "host-1", "status": "completed"},
    )

    entries = service.list_active_branch_entries(session_id=session.session_id)
    assert [entry.object.object_type for entry in entries] == [
        "history_compaction",
        "host_call_request",
        "host_call_response",
    ]


def test_load_missing_conversation_raises_domain_error(tmp_path: Path) -> None:
    service = _service(tmp_path)

    with pytest.raises(ConversationNotFoundError, match="missing"):
        service.load_conversation_session("missing")


def test_preview_archive_and_delete_use_conversation_facts(tmp_path: Path) -> None:
    service = _service(tmp_path)
    session = service.create_conversation_session(
        agent_id="Pickle",
        cwd=str(tmp_path),
    )
    service.append_user_message(
        session_id=session.session_id,
        message=UserMessage(content=[TextContent(text="hello")]),
    )
    service.append_host_call_request(
        session_id=session.session_id,
        content={"call_id": "host-1"},
    )
    service.append_assistant_message(
        session_id=session.session_id,
        message=AssistantMessage(content=[TextContent(text="final answer")]),
    )

    previews = service.list_conversation_previews(cwd=str(tmp_path))
    assert len(previews) == 1
    assert previews[0].message_count == 2
    assert previews[0].last_message == "final answer"

    service.archive_conversation_session(session_id=session.session_id)
    assert service.load_conversation_session(session.session_id).status == "archived"

    service.delete_conversation_session(session_id=session.session_id)
    with pytest.raises(ConversationNotFoundError, match="session-1"):
        service.load_conversation_session(session.session_id)
