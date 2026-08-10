"""会话活动分支到模型可见消息的投影。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Sequence

from pickel.conversations.agent_message import (
    AgentMessage,
    UserMessage,
    agent_message_from_dict,
)
from pickel.conversations.content_blocks import TextContent
from pickel.conversations.conversation_node import ConversationEntry

if TYPE_CHECKING:
    # 仅供旧 Runtime 在阶段 1 切换期间使用；迁移完成后连同 project_messages 删除。
    from pickel.conversations.session_entry import SessionEntry

_AGENT_MESSAGE = "agent_message"
_HISTORY_COMPACTION = "history_compaction"


@dataclass(frozen=True)
class _ProjectionEntry:
    node_id: str
    object_type: str
    content: dict[str, Any]


class ConversationProjector:
    """把持久化会话事实投影为 Provider-neutral 消息。"""

    def project_conversation_messages(
        self,
        entries: Sequence[ConversationEntry],
    ) -> list[AgentMessage]:
        return _project(
            [
                _ProjectionEntry(
                    node_id=entry.node.node_id,
                    object_type=entry.object.object_type,
                    content=entry.object.content,
                )
                for entry in entries
            ]
        )


def project_messages(entries: Sequence["SessionEntry"]) -> list[AgentMessage]:
    """阶段 1 迁移入口：把旧 SessionEntry 归一化后交给唯一投影内核。"""
    normalized: list[_ProjectionEntry] = []
    for entry in entries:
        content = dict(entry.payload) if isinstance(entry.payload, dict) else {}
        if entry.entry_type == "message":
            object_type = _AGENT_MESSAGE
        elif entry.entry_type == "compaction":
            object_type = _HISTORY_COMPACTION
            first_kept_entry_id = content.pop("first_kept_entry_id", None)
            if first_kept_entry_id is not None:
                content["first_kept_node_id"] = first_kept_entry_id
        else:
            object_type = entry.entry_type
        normalized.append(
            _ProjectionEntry(
                node_id=entry.entry_id,
                object_type=object_type,
                content=content,
            )
        )
    return _project(normalized)


def _project(entries: Sequence[_ProjectionEntry]) -> list[AgentMessage]:
    """唯一投影算法；输入已经归一化为目标态语义。"""
    if not entries:
        return []

    # 从路径尾部向前应用最后一个有效压缩；无效引用不掩盖更早的有效压缩。
    for index in range(len(entries) - 1, -1, -1):
        if entries[index].object_type != _HISTORY_COMPACTION:
            continue
        compacted = _project_with_compaction(entries, index)
        if compacted is not None:
            return compacted

    return _project_message_entries(entries)


def _resolve_first_kept_node_id(content: dict[str, Any]) -> str | None:
    first_kept_node_id = content.get("first_kept_node_id")
    if isinstance(first_kept_node_id, str) and first_kept_node_id:
        return first_kept_node_id
    return None


def _project_with_compaction(
    entries: Sequence[_ProjectionEntry],
    compaction_index: int,
) -> list[AgentMessage] | None:
    compaction = entries[compaction_index]
    first_kept_node_id = _resolve_first_kept_node_id(compaction.content)
    summary = compaction.content.get("summary")
    if first_kept_node_id is None:
        return None
    if not isinstance(summary, str):
        summary = "" if summary is None else str(summary)

    first_kept_index: int | None = None
    for index, entry in enumerate(entries):
        # first_kept 必须位于 compaction 插入点之前的祖先链上。
        if entry.node_id == first_kept_node_id and index < compaction_index:
            first_kept_index = index
            break
    if first_kept_index is None:
        return None

    messages: list[AgentMessage] = [
        UserMessage(content=[TextContent(text=f"[compaction]\n{summary}")]),
    ]
    messages.extend(_project_message_entries(entries[first_kept_index:]))
    return messages


def _project_message_entries(
    entries: Sequence[_ProjectionEntry],
) -> list[AgentMessage]:
    messages: list[AgentMessage] = []
    for entry in entries:
        if entry.object_type != _AGENT_MESSAGE:
            continue
        try:
            messages.append(agent_message_from_dict(entry.content))
        except (TypeError, ValueError, KeyError):
            # 损坏或未知 role 的消息事实不能阻断其余上下文投影。
            continue
    return messages
