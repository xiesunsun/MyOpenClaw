"""ConversationProjector：会话事实投影与压缩规则。"""

from __future__ import annotations

from pathlib import Path

from pickel.context.projection import ConversationProjector
from pickel.conversations.agent_message import AssistantMessage, UserMessage
from pickel.conversations.content_blocks import TextBlock
from pickel.conversations.conversation_service import ConversationService
from pickel.persistence.sqlite_runtime_store import SQLiteRuntimeStore


def _conversation(tmp_path: Path) -> tuple[ConversationService, str]:
    service = ConversationService(
        SQLiteRuntimeStore(tmp_path / "conversations.db"),
        session_id_factory=lambda: "session-1",
    )
    session = service.create_conversation_session(agent_id="Pickle")
    return service, session.session_id


def _project(service: ConversationService, session_id: str):
    entries = service.list_active_branch_entries(session_id=session_id)
    return ConversationProjector().project_conversation_messages(entries)


def test_project_conversation_messages_skips_non_message_facts(tmp_path: Path):
    service, session_id = _conversation(tmp_path)
    service.append_user_message(
        session_id=session_id,
        message=UserMessage(content=[TextBlock(text="hi")]),
    )
    service.append_host_call_request(
        session_id=session_id,
        content={"call_id": "host-1"},
    )
    service.append_host_call_response(
        session_id=session_id,
        content={"call_id": "host-1", "status": "completed"},
    )

    messages = _project(service, session_id)

    assert len(messages) == 1
    assert isinstance(messages[0], UserMessage)
    assert messages[0].content[0].text == "hi"


def test_compaction_keeps_from_first_kept_node_and_injects_summary(
    tmp_path: Path,
):
    service, session_id = _conversation(tmp_path)
    service.append_user_message(
        session_id=session_id,
        message=UserMessage(content=[TextBlock(text="u1")]),
    )
    service.append_assistant_message(
        session_id=session_id,
        message=AssistantMessage(content=[TextBlock(text="a1")]),
    )
    u2 = service.append_user_message(
        session_id=session_id,
        message=UserMessage(content=[TextBlock(text="u2")]),
    )
    service.append_assistant_message(
        session_id=session_id,
        message=AssistantMessage(content=[TextBlock(text="a2")]),
    )
    service.append_history_compaction(
        session_id=session_id,
        content={
            "summary": "earlier was about X",
            "first_kept_node_id": u2.node.node_id,
        },
    )

    messages = _project(service, session_id)

    assert [message.content[0].text for message in messages] == [
        "[compaction]\nearlier was about X",
        "u2",
        "a2",
    ]


def test_invalid_first_kept_node_ignores_compaction(tmp_path: Path):
    service, session_id = _conversation(tmp_path)
    service.append_user_message(
        session_id=session_id,
        message=UserMessage(content=[TextBlock(text="u1")]),
    )
    service.append_assistant_message(
        session_id=session_id,
        message=AssistantMessage(content=[TextBlock(text="a1")]),
    )
    service.append_history_compaction(
        session_id=session_id,
        content={
            "summary": "bad compact",
            "first_kept_node_id": "does-not-exist",
        },
    )

    assert [message.content[0].text for message in _project(service, session_id)] == [
        "u1",
        "a1",
    ]


def test_last_valid_compaction_wins(tmp_path: Path):
    service, session_id = _conversation(tmp_path)
    u1 = service.append_user_message(
        session_id=session_id,
        message=UserMessage(content=[TextBlock(text="u1")]),
    )
    service.append_assistant_message(
        session_id=session_id,
        message=AssistantMessage(content=[TextBlock(text="a1")]),
    )
    service.append_history_compaction(
        session_id=session_id,
        content={"summary": "old", "first_kept_node_id": u1.node.node_id},
    )
    u2 = service.append_user_message(
        session_id=session_id,
        message=UserMessage(content=[TextBlock(text="u2")]),
    )
    service.append_assistant_message(
        session_id=session_id,
        message=AssistantMessage(content=[TextBlock(text="a2")]),
    )
    service.append_history_compaction(
        session_id=session_id,
        content={"summary": "new", "first_kept_node_id": u2.node.node_id},
    )

    assert [message.content[0].text for message in _project(service, session_id)] == [
        "[compaction]\nnew",
        "u2",
        "a2",
    ]


def test_later_invalid_compaction_falls_back_to_earlier_valid(tmp_path: Path):
    service, session_id = _conversation(tmp_path)
    u1 = service.append_user_message(
        session_id=session_id,
        message=UserMessage(content=[TextBlock(text="u1")]),
    )
    service.append_assistant_message(
        session_id=session_id,
        message=AssistantMessage(content=[TextBlock(text="a1")]),
    )
    service.append_history_compaction(
        session_id=session_id,
        content={"summary": "valid", "first_kept_node_id": u1.node.node_id},
    )
    service.append_user_message(
        session_id=session_id,
        message=UserMessage(content=[TextBlock(text="u2")]),
    )
    service.append_history_compaction(
        session_id=session_id,
        content={"summary": "invalid", "first_kept_node_id": "missing"},
    )

    assert [message.content[0].text for message in _project(service, session_id)] == [
        "[compaction]\nvalid",
        "u1",
        "a1",
        "u2",
    ]
