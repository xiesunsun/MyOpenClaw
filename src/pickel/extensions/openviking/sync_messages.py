"""从 Session 活动路径提取可同步的消息序列。"""

from __future__ import annotations

from pickel.conversations.agent_message import (
    AgentMessage,
    agent_message_from_dict,
)
from pickel.conversations.session import Session
from pickel.conversations.session_entry import ENTRY_TYPE_MESSAGE


def list_syncable_agent_messages(session: Session) -> list[AgentMessage]:
    """活动路径上 entry_type=message 的 AgentMessage 列表（根 → leaf）。"""
    messages: list[AgentMessage] = []
    for entry in session.active_path():
        if entry.entry_type != ENTRY_TYPE_MESSAGE:
            continue
        messages.append(agent_message_from_dict(entry.payload))
    return messages
