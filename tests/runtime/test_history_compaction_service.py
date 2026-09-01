"""HistoryCompactionService 的 leaf、展开和 checkpoint CAS 合同。"""

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from pickel.conversations.agent_message import AssistantMessage, UserMessage
from pickel.conversations.content_blocks import TextBlock
from pickel.conversations.conversation_node import ConversationNode, HistoryCompaction
from pickel.runtime.history_compaction_service import HistoryCompactionService
from pickel.context.history_compaction import HistoryCompactionError


def _node(node_id, parent, content, content_type="agent_message"):
    return ConversationNode(
        node_id=node_id,
        session_id="session-1",
        parent_node_id=parent,
        content_type=content_type,
        content=content,
        created_at=datetime.now(timezone.utc),
    )


class _Conversations:
    def __init__(self, *, leaf="leaf-1", nodes=()):
        self.session = SimpleNamespace(active_node_id=leaf)
        self.nodes = tuple(nodes)
        self.appended = None

    def load_conversation_session(self, session_id):
        assert session_id == "session-1"
        return self.session

    def list_context_nodes(self, *, session_id, leaf_node_id):
        assert session_id == "session-1"
        assert leaf_node_id == self.session.active_node_id
        return list(self.nodes)

    def append_history_compaction_at_leaf(
        self, *, session_id, expected_leaf_node_id, content
    ):
        assert session_id == "session-1"
        assert expected_leaf_node_id == self.session.active_node_id
        self.appended = content
        return _node(
            "checkpoint-1", expected_leaf_node_id, content, "history_compaction"
        )


class _Generator:
    def __init__(self):
        self.kwargs = None

    async def generate(self, **kwargs):
        self.kwargs = kwargs
        return HistoryCompaction("new summary", (UserMessage((TextBlock("tail"),)),))


def _run(coro):
    return asyncio.run(coro)


def test_compact_expands_previous_summary_retained_tail_and_ledgers():
    previous = HistoryCompaction(
        "old summary",
        (UserMessage((TextBlock("retained"),)),),
        ("old.py",),
        ("changed.py",),
    )
    conversations = _Conversations(
        nodes=(
            _node("checkpoint", "old-leaf", previous, "history_compaction"),
            _node("leaf-1", "checkpoint", AssistantMessage((TextBlock("new"),))),
        )
    )
    generator = _Generator()
    service = HistoryCompactionService(conversations, generator)

    result = _run(
        service.compact(
            session_id="session-1",
            expected_leaf_node_id="leaf-1",
            model_context=None,
            send_summarizer=lambda **kwargs: None,
            max_summary_tokens=4096,
            preserve_tail_tokens=32000,
            worker_input_limit=64000,
        )
    )

    assert result.node_id == "checkpoint-1"
    assert generator.kwargs["previous_summary"] == "old summary"
    assert generator.kwargs["exact_messages"] == (
        previous.retained_messages[0],
        conversations.nodes[1].content,
    )
    assert generator.kwargs["previous_read_files"] == ("old.py",)
    assert generator.kwargs["previous_modified_files"] == ("changed.py",)
    assert generator.kwargs["worker_input_limit"] == 64000


def test_compact_rejects_expected_leaf_conflict_without_generator_or_append():
    conversations = _Conversations(leaf="new-leaf", nodes=())
    generator = _Generator()
    service = HistoryCompactionService(conversations, generator)

    with pytest.raises(HistoryCompactionError, match="leaf"):
        _run(
            service.compact(
                session_id="session-1",
                expected_leaf_node_id="old-leaf",
                model_context=None,
                send_summarizer=lambda **kwargs: None,
                max_summary_tokens=1,
                preserve_tail_tokens=1,
                worker_input_limit=1,
            )
        )
    assert generator.kwargs is None
    assert conversations.appended is None
