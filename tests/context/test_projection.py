from __future__ import annotations

from pathlib import Path

from pickel.context.projection import ConversationProjector
from pickel.conversations.agent_message import AssistantMessage, UserMessage
from pickel.conversations.content_blocks import TextBlock
from pickel.conversations.conversation_node import ConversationNode, HistoryCompaction
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
        service.list_context_nodes(
            session_id=session_id,
            leaf_node_id=service.load_conversation_session(session_id).active_node_id,
        )
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


def test_compaction_expands_retained_messages_without_node_lookup(tmp_path: Path):
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
        content=HistoryCompaction(
            "earlier was about X",
            (UserMessage(content=[TextBlock(text="retained")]),),
        ),
    )

    messages = _project(service, session.session_id)
    assert [message.content[0].text for message in messages] == [
        "[compaction]\nearlier was about X",
        "retained",
    ]


def test_projector_rejects_checkpoint_after_first_node(tmp_path: Path):
    message = UserMessage(content=[TextBlock(text="u1")])
    checkpoint = HistoryCompaction("summary", ())
    nodes = (
        ConversationNode("node-1", "session-1", None, "agent_message", message, _now()),
        ConversationNode(
            "node-2", "session-1", "node-1", "history_compaction", checkpoint, _now()
        ),
    )
    import pytest

    with pytest.raises(ValueError, match="输入合同无效"):
        ConversationProjector().project_conversation_messages(nodes)


def _now():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)
