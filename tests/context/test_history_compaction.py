from __future__ import annotations

from pathlib import Path

import pytest

from pickel.context.history_compaction import (
    commit_history_compaction,
    plan_history_compaction,
)
from pickel.context.projection import ConversationProjector
from pickel.conversations.agent_message import AssistantMessage, UserMessage
from pickel.conversations.content_blocks import TextContent
from pickel.conversations.conversation_service import ConversationService
from pickel.persistence.sqlite_runtime_store import SQLiteRuntimeStore


def _conversation(tmp_path: Path) -> tuple[ConversationService, str]:
    service = ConversationService(
        SQLiteRuntimeStore(tmp_path / "conversations.db"),
        session_id_factory=lambda: "session-1",
    )
    session = service.create_conversation_session(agent_id="Pickle")
    return service, session.session_id


def test_plan_and_commit_history_compaction_keeps_tail_units(tmp_path: Path):
    service, session_id = _conversation(tmp_path)
    for index in range(4):
        service.append_user_message(
            session_id=session_id,
            message=UserMessage(content=[TextContent(text=f"u{index}")]),
        )
        service.append_assistant_message(
            session_id=session_id,
            message=AssistantMessage(content=[TextContent(text=f"a{index}")]),
        )

    entries = service.list_active_branch_entries(session_id=session_id)
    plan = plan_history_compaction(
        entries,
        keep_units=4,
        summary="earlier dropped",
    )
    assert plan is not None
    committed = commit_history_compaction(
        service,
        session_id=session_id,
        plan=plan,
    )

    messages = ConversationProjector().project_conversation_messages(
        service.list_active_branch_entries(session_id=session_id)
    )
    assert committed.object.content["first_kept_node_id"] == plan.first_kept_node_id
    assert [message.content[0].text for message in messages] == [
        "[compaction]\nearlier dropped",
        "u2",
        "a2",
        "u3",
        "a3",
    ]


def test_plan_history_compaction_rejects_non_positive_window(tmp_path: Path):
    service, session_id = _conversation(tmp_path)
    entries = service.list_active_branch_entries(session_id=session_id)

    with pytest.raises(ValueError, match="keep_units"):
        plan_history_compaction(entries, keep_units=0, summary="invalid")
