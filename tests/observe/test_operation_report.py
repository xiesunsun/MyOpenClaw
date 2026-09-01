from __future__ import annotations

import html
import json
from datetime import datetime, timezone

from pickel.conversations.conversation_node import HistoryCompaction
from pickel.conversations.agent_message import AssistantMessage, UserMessage
from pickel.conversations.content_blocks import TextBlock
from pickel.conversations.conversation_service import ConversationService
from pickel.observe.operation_report import _html_document, export_operation_report
from pickel.persistence.in_memory_runtime_store import InMemoryRuntimeStore


def test_report_uses_current_session_and_conversation_node_contract(tmp_path) -> None:
    store = InMemoryRuntimeStore()
    service = ConversationService(
        store,
        session_id_factory=lambda: "report-session",
        node_id_factory=iter(
            ("user-node", "compaction-node", "assistant-node")
        ).__next__,
        now=lambda: datetime(2026, 8, 26, tzinfo=timezone.utc),
    )
    service.create_conversation_session(agent_id="Pickle", cwd=str(tmp_path))
    service.append_user_message(
        session_id="report-session",
        message=UserMessage((TextBlock("hello"),)),
    )
    service.append_history_compaction(
        session_id="report-session",
        content=HistoryCompaction("summary", (UserMessage((TextBlock("hello"),)),)),
    )
    service.append_assistant_message(
        session_id="report-session",
        message=AssistantMessage((TextBlock("world"),)),
    )
    session = service.load_conversation_session("report-session")

    path = export_operation_report(
        conversation_service=service,
        sessions=(session,),
        out=tmp_path / "report.html",
    )

    document = path.read_text(encoding="utf-8")
    encoded = document.split("<pre data-report-json>", 1)[1].split("</pre>", 1)[0]
    payload = json.loads(html.unescape(encoded))
    exported = payload[0]
    assert exported["session"] == {
        "session_id": "report-session",
        "agent_id": "Pickle",
        "workspace_id": session.workspace_id,
        "cwd": str(tmp_path.resolve()),
        "active_node_id": "assistant-node",
        "active_operation_id": None,
        "title": None,
        "created_at": "2026-08-26T00:00:00+00:00",
        "updated_at": "2026-08-26T00:00:00+00:00",
        "archived_at": None,
    }
    assert [message["node_id"] for message in exported["messages"]] == [
        "user-node",
        "assistant-node",
    ]
    assert exported["messages"][1]["parent_node_id"] == "compaction-node"
    assert [message["role"] for message in exported["messages"]] == [
        "user",
        "assistant",
    ]
    assert "status" not in exported["session"]
    assert "commit_sequence" not in exported["session"]
    assert "Operation Report" in document
    assert "对话" in document
    assert "执行事件" in document
    assert "hello" in document
    assert "world" in document


def test_report_surfaces_runtime_failure_without_hiding_raw_record() -> None:
    payload = [
        {
            "session": {
                "session_id": "session-1",
                "agent_id": "Pickle",
                "workspace_id": "workspace-1",
                "cwd": "/workspace",
                "active_operation_id": None,
                "title": None,
                "updated_at": "2026-08-26T00:00:00+00:00",
            },
            "messages": [],
            "events": [
                {
                    "record_type": "runtime_event",
                    "event_type": "agent_run_failed",
                    "operation_id": "pending",
                    "occurred_at": "2026-08-26T00:00:01+00:00",
                    "error_type": "NameError",
                    "message": "missing delta type",
                }
            ],
        }
    ]

    document = _html_document(payload)

    assert '<span class="health danger">异常</span>' in document
    assert "agent_run_failed" in document
    assert "missing delta type" in document
    encoded = document.split("<pre data-report-json>", 1)[1].split("</pre>", 1)[0]
    assert json.loads(html.unescape(encoded)) == payload
