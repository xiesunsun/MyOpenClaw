"""SessionEntry 树与 active_path 不变量测试。"""

from __future__ import annotations

import pytest

from myopenclaw.conversations.agent_message import (
    AssistantMessage,
    ToolResultMessage,
    UserMessage,
    agent_message_to_dict,
)
from myopenclaw.conversations.content_blocks import TextContent, ToolCallContent
from myopenclaw.conversations.session import Session
from myopenclaw.conversations.session_entry import (
    ENTRY_TYPE_COMPACTION,
    ENTRY_TYPE_MESSAGE,
    SessionEntry,
)


def test_append_chain_and_active_path():
    session = Session.create(agent_id="Pickle")
    u = session.append_user(UserMessage(content=[TextContent(text="hi")]))
    a = session.append_assistant(
        AssistantMessage(content=[ToolCallContent(id="c1", name="t", arguments={})])
    )
    t = session.append_tool_result(
        ToolResultMessage(tool_call_id="c1", tool_name="t", content=[TextContent(text="ok")])
    )
    path = session.active_path()
    assert [e.entry_id for e in path] == [u.entry_id, a.entry_id, t.entry_id]
    assert session.leaf_id == t.entry_id
    assert path[0].parent_id is None
    assert path[1].parent_id == u.entry_id
    assert path[2].parent_id == a.entry_id


def test_entries_are_append_only():
    session = Session.create(agent_id="Pickle")
    e = session.append_user(UserMessage(content=[TextContent(text="x")]))
    # 不应提供 mutate payload 的公开 API；payload 为不可变结构
    assert e.entry_type == ENTRY_TYPE_MESSAGE
    assert isinstance(e, SessionEntry)
    with pytest.raises(Exception):
        e.entry_type = "compaction"  # type: ignore[misc]
    with pytest.raises(Exception):
        e.payload = {}  # type: ignore[misc]


def test_message_payload_is_versioned_agent_message():
    session = Session.create(agent_id="Pickle")
    msg = UserMessage(content=[TextContent(text="hello")])
    entry = session.append_user(msg)
    assert entry.payload == agent_message_to_dict(msg)
    assert entry.payload["payload_version"] == 1
    assert entry.payload["role"] == "user"


def test_append_updates_leaf_and_updated_at():
    session = Session.create(agent_id="Pickle")
    before = session.updated_at
    assert session.leaf_id is None
    assert session.active_path() == []

    entry = session.append_user(UserMessage(content=[TextContent(text="a")]))
    assert session.leaf_id == entry.entry_id
    assert session.updated_at >= before
    assert len(session.entries) == 1


def test_branch_via_move_leaf_then_append():
    session = Session.create(agent_id="Pickle")
    root = session.append_user(UserMessage(content=[TextContent(text="root")]))
    branch_a = session.append_assistant(
        AssistantMessage(content=[TextContent(text="path-a")])
    )
    assert session.leaf_id == branch_a.entry_id

    # 切回 root 后开新分支
    session.move_leaf(root.entry_id)
    assert session.leaf_id == root.entry_id
    branch_b = session.append_assistant(
        AssistantMessage(content=[TextContent(text="path-b")])
    )

    assert branch_b.parent_id == root.entry_id
    assert session.leaf_id == branch_b.entry_id
    path = session.active_path()
    assert [e.entry_id for e in path] == [root.entry_id, branch_b.entry_id]
    # 旧分支仍在 entries 中，但不在 active_path
    assert branch_a in session.entries
    assert branch_a.entry_id not in {e.entry_id for e in path}


def test_move_leaf_rejects_unknown_entry():
    session = Session.create(agent_id="Pickle")
    session.append_user(UserMessage(content=[TextContent(text="x")]))
    with pytest.raises(ValueError, match="entry"):
        session.move_leaf("does-not-exist")


def test_append_compaction_on_chain():
    session = Session.create(agent_id="Pickle")
    u = session.append_user(UserMessage(content=[TextContent(text="old")]))
    a = session.append_assistant(AssistantMessage(content=[TextContent(text="reply")]))
    compaction_payload = {
        "payload_version": 1,
        "summary": "compressed history",
        "first_kept_entry_id": a.entry_id,
    }
    c = session.append_compaction(compaction_payload)
    assert c.entry_type == ENTRY_TYPE_COMPACTION
    assert c.parent_id == a.entry_id
    assert session.leaf_id == c.entry_id
    assert c.payload["summary"] == "compressed history"
    assert c.payload["payload_version"] == 1
    path = session.active_path()
    assert [e.entry_id for e in path] == [u.entry_id, a.entry_id, c.entry_id]


def test_session_create_defaults_for_entry_tree():
    session = Session.create(agent_id="Pickle", session_id="session-1")
    assert session.session_id == "session-1"
    assert session.agent_id == "Pickle"
    assert session.status == "active"
    assert session.leaf_id is None
    assert session.entries == []
    assert session.title is None
    assert not hasattr(session, "messages")
    assert not hasattr(session, "remote_session_id")
    assert not hasattr(session, "openviking_account_id")
