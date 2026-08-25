from __future__ import annotations

from pathlib import Path

from pickel.context.projection import ConversationProjector
from pickel.conversations.agent_message import AssistantMessage, UserMessage
from pickel.conversations.content_blocks import TextBlock
from pickel.conversations.conversation_node import HistoryCompaction
from pickel.conversations.conversation_service import ConversationService
from pickel.persistence.in_memory_runtime_store import InMemoryRuntimeStore


def _conversation(tmp_path: Path) -> ConversationService:
    return ConversationService(
        InMemoryRuntimeStore(),
        session_id_factory=lambda: "session-1",
        node_id_factory=_ids(),
    )


def _ids():
    values = iter(f"node-{index}" for index in range(1, 30))
    return values.__next__


def _project(service: ConversationService, session_id: str):
    return ConversationProjector().project_conversation_messages(
        service.list_active_branch_nodes(session_id=session_id)
    )


def test_project_fixed_leaf_returns_only_agent_messages(tmp_path: Path):
    service = _conversation(tmp_path)
    session = service.create_conversation_session(agent_id="Pickle", cwd=str(tmp_path))
    service.append_user_message(
        session_id=session.session_id,
        message=UserMessage(content=[TextBlock(text="hi")]),
    )
    assert [
        message.content[0].text for message in _project(service, session.session_id)
    ] == ["hi"]


def test_compaction_keeps_from_first_kept_node_and_injects_summary(tmp_path: Path):
    service = _conversation(tmp_path)
    session = service.create_conversation_session(agent_id="Pickle", cwd=str(tmp_path))
    service.append_user_message(
        session_id=session.session_id,
        message=UserMessage(content=[TextBlock(text="u1")]),
    )
    service.append_assistant_message(
        session_id=session.session_id,
        message=AssistantMessage(content=[TextBlock(text="a1")]),
    )
    nodes = service.list_active_branch_nodes(session_id=session.session_id)
    kept = nodes[0]
    service.append_user_message(
        session_id=session.session_id,
        message=UserMessage(content=[TextBlock(text="u2")]),
    )
    service.append_assistant_message(
        session_id=session.session_id,
        message=AssistantMessage(content=[TextBlock(text="a2")]),
    )
    service.append_history_compaction(
        session_id=session.session_id,
        content=HistoryCompaction("earlier was about X", kept.node_id),
    )

    messages = _project(service, session.session_id)
    assert [message.content[0].text for message in messages] == [
        "[compaction]\nearlier was about X",
        "u1",
        "a1",
        "u2",
        "a2",
    ]


def test_invalid_compaction_is_ignored(tmp_path: Path):
    service = _conversation(tmp_path)
    session = service.create_conversation_session(agent_id="Pickle", cwd=str(tmp_path))
    service.append_user_message(
        session_id=session.session_id,
        message=UserMessage(content=[TextBlock(text="u1")]),
    )
    service.append_history_compaction(
        session_id=session.session_id,
        content=HistoryCompaction("bad", "missing"),
    )
    assert [
        message.content[0].text for message in _project(service, session.session_id)
    ] == ["u1"]
