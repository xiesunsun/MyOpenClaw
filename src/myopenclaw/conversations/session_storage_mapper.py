"""Session / SessionEntry ↔ SQLite 行映射。

payload_json 存 AgentMessage（或 compaction 等）dict；不再映射 ToolCallBatch / SessionMessage。
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any

from myopenclaw.conversations.session import Session
from myopenclaw.conversations.session_entry import ENTRY_TYPE_MESSAGE, SessionEntry
from myopenclaw.conversations.session_preview import (
    SessionPreview,
    preview_text_from_message_payload,
)


def session_to_metadata_record(session: Session) -> dict[str, Any]:
    return {
        "session_id": session.session_id,
        "agent_id": session.agent_id,
        "cwd": session.cwd,
        "leaf_id": session.leaf_id,
        "created_at": _datetime_to_storage(session.created_at),
        "updated_at": _datetime_to_storage(session.updated_at),
        "status": session.status,
        "title": session.title,
    }


def session_entry_to_record(entry: SessionEntry) -> dict[str, Any]:
    return {
        "entry_id": entry.entry_id,
        "session_id": entry.session_id,
        "parent_id": entry.parent_id,
        "entry_type": entry.entry_type,
        "payload_json": json.dumps(entry.payload, ensure_ascii=False),
        "created_at": _datetime_to_storage(entry.created_at),
    }


def session_entry_from_record(record: Mapping[str, Any]) -> SessionEntry:
    payload = json.loads(str(record["payload_json"]))
    if not isinstance(payload, dict):
        raise TypeError("entry payload_json 必须是 JSON object")
    return SessionEntry(
        entry_id=str(record["entry_id"]),
        session_id=str(record["session_id"]),
        parent_id=_optional_str(record["parent_id"]),
        entry_type=str(record["entry_type"]),
        payload=payload,
        created_at=_datetime_from_storage_required(record["created_at"]),
    )


def session_from_storage(
    *,
    session_record: Mapping[str, Any],
    entry_records: Iterable[Mapping[str, Any]],
) -> Session:
    return Session(
        session_id=str(session_record["session_id"]),
        agent_id=str(session_record["agent_id"]),
        cwd=str(session_record["cwd"]),
        leaf_id=_optional_str(session_record["leaf_id"]),
        entries=[session_entry_from_record(record) for record in entry_records],
        created_at=_datetime_from_storage_required(session_record["created_at"]),
        updated_at=_datetime_from_storage_required(session_record["updated_at"]),
        status=str(session_record["status"]),
        title=_optional_str(session_record["title"]),
    )


def build_session_preview(*, session: Session) -> SessionPreview:
    path = session.active_path()
    message_entries = [entry for entry in path if entry.entry_type == ENTRY_TYPE_MESSAGE]
    last_message = ""
    if message_entries:
        last_message = preview_text_from_message_payload(message_entries[-1].payload)
    return SessionPreview(
        session_id=session.session_id,
        agent_id=session.agent_id,
        created_at=session.created_at,
        updated_at=session.updated_at,
        status=session.status,
        message_count=len(message_entries),
        last_message=last_message,
        cwd=session.cwd,
    )


def session_preview_from_storage_record(record: Mapping[str, Any]) -> SessionPreview:
    """从 list 查询聚合行构建预览（message_count / last_payload 已按 active_path 计算）。"""
    last_payload_json = record["last_payload_json"]
    last_message = ""
    if last_payload_json is not None:
        payload = json.loads(str(last_payload_json))
        if isinstance(payload, dict):
            last_message = preview_text_from_message_payload(payload)
    return SessionPreview(
        session_id=str(record["session_id"]),
        agent_id=str(record["agent_id"]),
        created_at=_datetime_from_storage_required(record["created_at"]),
        updated_at=_datetime_from_storage_required(record["updated_at"]),
        status=str(record["status"]),
        message_count=int(record["message_count"]),
        last_message=last_message,
        cwd=str(record.get("cwd") or ""),
    )


def _datetime_to_storage(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat()


def _datetime_from_storage_required(value: Any) -> datetime:
    if value is None:
        raise ValueError("时间字段不能为空")
    return datetime.fromisoformat(str(value))


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)
