"""Session 列表封面与 last_message 展示规则。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from pickel.conversations.agent_message import AgentMessage, agent_message_to_dict
from pickel.conversations.conversation_node import ConversationNode
from pickel.conversations.conversation_session import ConversationSession


def _normalize_whitespace(value: str) -> str:
    return " ".join(value.split())


def _truncate(value: str, *, limit: int = 50) -> str:
    if len(value) <= limit:
        return value
    return f"{value[:limit]}..."


def preview_text_from_message(message: AgentMessage) -> str:
    """从类型化 AgentMessage 生成 last_message 文本。"""
    payload = agent_message_to_dict(message)
    content = payload.get("content") or []
    texts: list[str] = []
    tool_names: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text" and block.get("text"):
            texts.append(str(block["text"]))
        elif block.get("type") == "tool_call" and block.get("name"):
            tool_names.append(str(block["name"]))
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
        object.__setattr__(
            self, "last_message", _truncate(_normalize_whitespace(self.last_message))
        )


def build_conversation_preview(
    *, session: ConversationSession, nodes: list[ConversationNode]
) -> SessionPreview:
    """从固定 active leaf 的 Node 路径投影展示封面。"""
    messages = [node.content for node in nodes if node.content_type == "agent_message"]
    last_message = preview_text_from_message(messages[-1]) if messages else ""
    status = (
        "archived"
        if session.archived_at is not None
        else ("running" if session.active_operation_id is not None else "idle")
    )
    return SessionPreview(
        session_id=session.session_id,
        agent_id=session.agent_id,
        created_at=session.created_at,
        updated_at=session.updated_at,
        status=status,
        message_count=len(messages),
        last_message=last_message,
        cwd=str(session.cwd),
    )
