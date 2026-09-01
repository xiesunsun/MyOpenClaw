from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from pickel.conversations.agent_message import (
    AssistantMessage,
    ToolResultMessage,
    UserMessage,
)
from pickel.conversations.content_blocks import TextBlock
from pickel.conversations.conversation_service import (
    ConversationNotFoundError,
    ConversationService,
)
from pickel.conversations.conversation_node import HistoryCompaction
from pickel.persistence.in_memory_runtime_store import InMemoryRuntimeStore


def _service(tmp_path: Path) -> ConversationService:
    return ConversationService(
        InMemoryRuntimeStore(),
        session_id_factory=lambda: "session-1",
        node_id_factory=iter(("node-1", "node-2", "node-3", "node-4")).__next__,
        now=lambda: datetime(2026, 8, 11, tzinfo=timezone.utc),
    )


def test_create_and_append_typed_nodes(tmp_path: Path) -> None:
    service = _service(tmp_path)
    session = service.create_conversation_session(agent_id="Pickle", cwd=str(tmp_path))

    user_node = service.append_user_message(
        session_id=session.session_id,
        message=UserMessage(content=[TextBlock(text="hello")]),
    )
    assistant_node = service.append_assistant_message(
        session_id=session.session_id,
        message=AssistantMessage(content=[TextBlock(text="hi")]),
    )
    tool_node = service.append_tool_result_message(
        session_id=session.session_id,
        message=ToolResultMessage(
            tool_call_id="call-1", tool_name="echo", content=[TextBlock(text="done")]
        ),
    )

    nodes = service.list_active_branch_nodes(session_id=session.session_id)
    assert [node.node_id for node in nodes] == ["node-1", "node-2", "node-3"]
    assert nodes[0].content == user_node.content
    assert nodes[1].content == assistant_node.content
    assert nodes[2].content == tool_node.content


def test_same_cwd_reuses_deterministic_workspace(tmp_path: Path) -> None:
    store = InMemoryRuntimeStore()
    session_ids = iter(("session-1", "session-2"))
    service = ConversationService(store, session_id_factory=session_ids.__next__)

    first = service.create_conversation_session(agent_id="Pickle", cwd=str(tmp_path))
    second = service.create_conversation_session(agent_id="Pickle", cwd=str(tmp_path))

    assert first.workspace_id == second.workspace_id
    assert store.find_workspace_by_root(tmp_path) is not None


def test_move_active_leaf_uses_node_cas(tmp_path: Path) -> None:
    service = _service(tmp_path)
    session = service.create_conversation_session(agent_id="Pickle", cwd=str(tmp_path))
    first = service.append_user_message(
        session_id=session.session_id,
        message=UserMessage(content=[TextBlock(text="first")]),
    )
    service.append_assistant_message(
        session_id=session.session_id,
        message=AssistantMessage(content=[TextBlock(text="discarded branch")]),
    )

    moved = service.move_active_branch_to(
        session_id=session.session_id, node_id=first.node_id
    )
    assert moved.active_node_id == first.node_id


def test_list_branch_nodes_uses_explicit_leaf(tmp_path: Path) -> None:
    service = _service(tmp_path)
    session = service.create_conversation_session(agent_id="Pickle", cwd=str(tmp_path))
    first = service.append_user_message(
        session_id=session.session_id,
        message=UserMessage(content=[TextBlock(text="first")]),
    )
    second = service.append_assistant_message(
        session_id=session.session_id,
        message=AssistantMessage(content=[TextBlock(text="second")]),
    )

    nodes = service.list_branch_nodes(
        session_id=session.session_id, leaf_node_id=first.node_id
    )

    assert [node.node_id for node in nodes] == [first.node_id]
    assert second.node_id not in [node.node_id for node in nodes]


def test_append_cas_failure_does_not_leave_node(tmp_path: Path) -> None:
    class RejectingStore(InMemoryRuntimeStore):
        def append_node(self, *, node, expected_node_id):
            return False

    store = RejectingStore()
    service = ConversationService(
        store,
        session_id_factory=lambda: "session-1",
        node_id_factory=lambda: "node-rejected",
    )
    session = service.create_conversation_session(agent_id="Pickle", cwd=str(tmp_path))

    with pytest.raises(RuntimeError, match="CAS"):
        service.append_user_message(
            session_id=session.session_id,
            message=UserMessage(content=[TextBlock(text="not committed")]),
        )

    assert store.load_node("node-rejected") is None
    assert service.list_active_branch_nodes(session_id=session.session_id) == []


def test_append_compaction_preserves_typed_content(tmp_path: Path) -> None:
    service = _service(tmp_path)
    session = service.create_conversation_session(agent_id="Pickle", cwd=str(tmp_path))
    node = service.append_history_compaction(
        session_id=session.session_id,
        content=HistoryCompaction(summary="short", retained_messages=()),
    )
    assert node.content_type == "history_compaction"
    assert node.content == HistoryCompaction(summary="short", retained_messages=())


def test_preview_archive_and_delete(tmp_path: Path) -> None:
    service = _service(tmp_path)
    session = service.create_conversation_session(agent_id="Pickle", cwd=str(tmp_path))
    service.append_user_message(
        session_id=session.session_id,
        message=UserMessage(content=[TextBlock(text="hello")]),
    )
    service.append_assistant_message(
        session_id=session.session_id,
        message=AssistantMessage(content=[TextBlock(text="final answer")]),
    )

    previews = service.list_conversation_previews(cwd=str(tmp_path))
    assert previews[0].message_count == 2
    assert previews[0].last_message == "final answer"
    assert previews[0].status == "idle"

    service.archive_conversation_session(session_id=session.session_id)
    assert service.list_conversation_previews(cwd=str(tmp_path))[0].status == "archived"
    service.delete_conversation_session(session_id=session.session_id)
    with pytest.raises(ConversationNotFoundError, match="session-1"):
        service.load_conversation_session(session.session_id)


def test_load_missing_conversation_raises_domain_error(tmp_path: Path) -> None:
    with pytest.raises(ConversationNotFoundError, match="missing"):
        _service(tmp_path).load_conversation_session("missing")
