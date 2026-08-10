"""Session 列表封面与 last_message 展示规则。

预览只消费 AgentMessage 形状的 payload dict（content blocks），
仅识别 AgentMessage 与 compaction 等持久化 payload。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pickel.conversations.conversation_node import ConversationEntry
from pickel.conversations.conversation_session import ConversationSession


def _normalize_whitespace(value: str) -> str:
    return " ".join(value.split())


def _truncate(value: str, *, limit: int = 50) -> str:
    if len(value) <= limit:
        return value
    return f"{value[:limit]}..."


def preview_text_from_message_payload(payload: Mapping[str, Any]) -> str:
    """从 AgentMessage payload 生成 last_message 原文（截断由 SessionPreview 负责）。

    规则：
    - 优先拼接 text content（user / assistant / tool result 均适用）
    - 无 text 且含 tool_call → ``[tools] name1, name2``
    - 否则空串
    """
    content = payload.get("content") or []
    if not isinstance(content, list):
        return ""

    texts: list[str] = []
    tool_names: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "text":
            text = block.get("text")
            if text:
                texts.append(str(text))
        elif block_type == "tool_call":
            name = block.get("name")
            if name:
                tool_names.append(str(name))

    joined = _normalize_whitespace(" ".join(texts))
    if joined:
        return joined
    if tool_names:
        return f"[tools] {', '.join(tool_names)}"
    return ""


@dataclass(frozen=True)
class SessionPreview:
    session_id: str
    agent_id: str
    created_at: datetime
    updated_at: datetime
    status: str
    message_count: int
    last_message: str
    cwd: str = ""

    def __post_init__(self) -> None:
        normalized = _truncate(_normalize_whitespace(self.last_message))
        object.__setattr__(self, "last_message", normalized)


def build_conversation_preview(
    *,
    session: ConversationSession,
    entries: list[ConversationEntry],
) -> SessionPreview:
    """从会话只读视图和活动分支事实投影展示封面。"""
    message_entries = [
        entry for entry in entries if entry.object.object_type == "agent_message"
    ]
    last_message = ""
    if message_entries:
        last_message = preview_text_from_message_payload(
            message_entries[-1].object.content
        )
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
