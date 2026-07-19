"""project_messages：message 投影与 compaction 规则。"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from myopenclaw.context.projection import project_messages
from myopenclaw.conversations.agent_message import (
    AssistantMessage,
    UserMessage,
)
from myopenclaw.conversations.content_blocks import TextContent
from myopenclaw.conversations.session import Session
from myopenclaw.conversations.session_entry import SessionEntry


def _append_unknown_entry(session: Session, *, entry_type: str, payload: dict) -> SessionEntry:
    entry = SessionEntry(
        entry_id=str(uuid4()),
        session_id=session.session_id,
        parent_id=session.leaf_id,
        entry_type=entry_type,
        payload=payload,
        created_at=datetime.now(timezone.utc),
    )
    session.entries.append(entry)
    session.leaf_id = entry.entry_id
    session.touch(at=entry.created_at)
    return entry


def test_project_messages_skips_unknown_entry_types():
    session = Session.create(agent_id="Pickle")
    session.append_user(UserMessage(content=[TextContent(text="hi")]))
    _append_unknown_entry(
        session,
        entry_type="openviking",
        payload={"kind": "binding", "remote_session_id": "r1"},
    )
    _append_unknown_entry(
        session,
        entry_type="model_change",
        payload={"model": "claude-test"},
    )

    messages = project_messages(session.active_path())

    assert len(messages) == 1
    assert isinstance(messages[0], UserMessage)
    assert messages[0].content[0].text == "hi"


def test_compaction_keeps_from_first_kept_and_injects_summary():
    session = Session.create(agent_id="Pickle")
    session.append_user(UserMessage(content=[TextContent(text="u1")]))
    session.append_assistant(AssistantMessage(content=[TextContent(text="a1")]))
    u2 = session.append_user(UserMessage(content=[TextContent(text="u2")]))
    session.append_assistant(AssistantMessage(content=[TextContent(text="a2")]))
    session.append_compaction(
        {
            "summary": "earlier was about X",
            "first_kept_entry_id": u2.entry_id,
        }
    )

    messages = project_messages(session.active_path())

    assert len(messages) == 3
    assert isinstance(messages[0], UserMessage)
    assert messages[0].content[0].text == "[compaction]\nearlier was about X"
    assert isinstance(messages[1], UserMessage)
    assert messages[1].content[0].text == "u2"
    assert isinstance(messages[2], AssistantMessage)
    assert messages[2].content[0].text == "a2"


def test_invalid_first_kept_ignores_compaction():
    session = Session.create(agent_id="Pickle")
    session.append_user(UserMessage(content=[TextContent(text="u1")]))
    session.append_assistant(AssistantMessage(content=[TextContent(text="a1")]))
    session.append_compaction(
        {
            "summary": "bad compact",
            "first_kept_entry_id": "does-not-exist",
        }
    )

    messages = project_messages(session.active_path())

    assert len(messages) == 2
    assert messages[0].content[0].text == "u1"
    assert messages[1].content[0].text == "a1"


def test_last_compaction_wins_when_multiple():
    session = Session.create(agent_id="Pickle")
    u1 = session.append_user(UserMessage(content=[TextContent(text="u1")]))
    session.append_assistant(AssistantMessage(content=[TextContent(text="a1")]))
    u2 = session.append_user(UserMessage(content=[TextContent(text="u2")]))
    session.append_assistant(AssistantMessage(content=[TextContent(text="a2")]))
    session.append_compaction(
        {
            "summary": "old summary",
            "first_kept_entry_id": u1.entry_id,
        }
    )
    u3 = session.append_user(UserMessage(content=[TextContent(text="u3")]))
    session.append_assistant(AssistantMessage(content=[TextContent(text="a3")]))
    session.append_compaction(
        {
            "summary": "new summary",
            "first_kept_entry_id": u3.entry_id,
        }
    )

    messages = project_messages(session.active_path())

    assert messages[0].content[0].text == "[compaction]\nnew summary"
    assert [m.content[0].text for m in messages[1:]] == ["u3", "a3"]
