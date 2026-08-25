from __future__ import annotations

from pathlib import Path

import pytest

from pickel.context.history_compaction import (
    commit_history_compaction,
    plan_history_compaction,
)
from pickel.context.projection import ConversationProjector
from pickel.conversations.agent_message import AssistantMessage, UserMessage
from pickel.conversations.content_blocks import TextBlock
from pickel.conversations.conversation_service import ConversationService
from pickel.persistence.in_memory_runtime_store import InMemoryRuntimeStore


def _conversation(tmp_path: Path) -> ConversationService:
    values = iter(f"node-{index}" for index in range(1, 30))
    return ConversationService(
        InMemoryRuntimeStore(),
        session_id_factory=lambda: "session-1",
        node_id_factory=values.__next__,
    )


def test_plan_and_commit_history_compaction_keeps_tail_units(tmp_path: Path):
    service = _conversation(tmp_path)
    session = service.create_conversation_session(agent_id="Pickle", cwd=str(tmp_path))
    for index in range(4):
        service.append_user_message(
            session_id=session.session_id,
            message=UserMessage(content=[TextBlock(text=f"u{index}")]),
        )
        service.append_assistant_message(
            session_id=session.session_id,
            message=AssistantMessage(content=[TextBlock(text=f"a{index}")]),
        )

    nodes = service.list_active_branch_nodes(session_id=session.session_id)
    plan = plan_history_compaction(nodes, keep_turns=2, summary="earlier dropped")
    assert plan is not None
    committed = commit_history_compaction(
        service, session_id=session.session_id, plan=plan
    )

    messages = ConversationProjector().project_conversation_messages(
        service.list_active_branch_nodes(session_id=session.session_id)
    )
    assert committed.content.first_kept_node_id == plan.first_kept_node_id
    assert [message.content[0].text for message in messages] == [
        "[compaction]\nearlier dropped",
        "u2",
        "a2",
        "u3",
        "a3",
    ]


def test_plan_history_compaction_rejects_non_positive_window(tmp_path: Path):
    service = _conversation(tmp_path)
    session = service.create_conversation_session(agent_id="Pickle", cwd=str(tmp_path))
    with pytest.raises(ValueError, match="keep_turns"):
        plan_history_compaction(
            service.list_active_branch_nodes(session_id=session.session_id),
            keep_turns=0,
            summary="invalid",
        )
