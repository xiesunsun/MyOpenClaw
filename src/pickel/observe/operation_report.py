"""从 Conversation 事实与 Operation Event 轨迹导出自包含报告。"""

from __future__ import annotations

import html
import json
from pathlib import Path

from pickel.config.paths import home_dir
from pickel.conversations.agent_message import agent_message_from_dict
from pickel.conversations.conversation_service import ConversationService
from pickel.conversations.conversation_session import ConversationSession
from pickel.observe.jsonl_trace_sink import trace_path


def export_operation_report(
    *,
    conversation_service: ConversationService,
    sessions: tuple[ConversationSession, ...],
    out: Path | None = None,
) -> Path:
    if not sessions:
        raise ValueError("至少需要一个 ConversationSession")
    target = out or (home_dir() / "observations" / f"{sessions[0].session_id}.html")
    target = target.expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = []
    for session in sessions:
        entries = conversation_service.list_active_branch_entries(
            session_id=session.session_id
        )
        messages = []
        for entry in entries:
            if entry.object.object_type != "agent_message":
                continue
            try:
                message = agent_message_from_dict(entry.object.content)
            except (KeyError, TypeError, ValueError):
                continue
            messages.append(
                {
                    "node_id": entry.node.node_id,
                    "message": entry.object.content,
                    "role": message.role,
                }
            )
        payload.append(
            {
                "session": {
                    "session_id": session.session_id,
                    "agent_id": session.agent_id,
                    "status": session.status,
                    "cwd": session.cwd,
                    "commit_sequence": session.current_commit_sequence,
                },
                "messages": messages,
                "events": _read_trace_events(trace_path(session.session_id)),
            }
        )
    encoded = json.dumps(payload, ensure_ascii=False, indent=2)
    target.write_text(_html_document(encoded), encoding="utf-8")
    return target


def _read_trace_events(path: Path) -> list[dict]:
    if not path.exists():
        return []
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            events.append(value)
    return events


def _html_document(encoded: str) -> str:
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Pickel Operation Report</title>
<style>body{{font:14px ui-monospace,monospace;margin:2rem;max-width:1200px}}
pre{{white-space:pre-wrap;overflow-wrap:anywhere;background:#f6f8fa;padding:1rem}}</style>
</head><body><h1>Pickel Operation Report</h1>
<p>Conversation facts and derived runtime events. Recovery uses persisted Operation State, not this report.</p>
<pre>{html.escape(encoded)}</pre></body></html>"""
