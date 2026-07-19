"""SessionEntry active path → list[AgentMessage] 投影。

规则：
- 仅展开 entry_type=message；未知类型（openviking / model_change 等）跳过
- 活动路径上最后一条有效 compaction：注入 UserMessage("[compaction]\\n{summary}")，
  丢弃 first_kept 之前的展开；无效 first_kept 则忽略该 compaction
"""

from __future__ import annotations

from myopenclaw.conversations.agent_message import (
    AgentMessage,
    UserMessage,
    agent_message_from_dict,
)
from myopenclaw.conversations.content_blocks import TextContent
from myopenclaw.conversations.session_entry import (
    ENTRY_TYPE_COMPACTION,
    ENTRY_TYPE_MESSAGE,
    SessionEntry,
)


def project_messages(entries: list[SessionEntry]) -> list[AgentMessage]:
    """将 active path 投影为模型可见的 AgentMessage 列表。"""
    if not entries:
        return []

    last_compaction_index: int | None = None
    for index, entry in enumerate(entries):
        if entry.entry_type == ENTRY_TYPE_COMPACTION:
            last_compaction_index = index

    if last_compaction_index is not None:
        compacted = _project_with_compaction(entries, last_compaction_index)
        if compacted is not None:
            return compacted

    return _project_message_entries(entries)


def _project_with_compaction(
    entries: list[SessionEntry],
    compaction_index: int,
) -> list[AgentMessage] | None:
    """应用最后一条 compaction；无效时返回 None 以降级为全量投影。"""
    compaction = entries[compaction_index]
    payload = compaction.payload if isinstance(compaction.payload, dict) else {}
    first_kept_id = payload.get("first_kept_entry_id")
    summary = payload.get("summary")
    if not isinstance(first_kept_id, str) or not first_kept_id:
        return None
    if not isinstance(summary, str):
        summary = "" if summary is None else str(summary)

    first_kept_index: int | None = None
    for index, entry in enumerate(entries):
        # first_kept 须位于 compaction 插入点之前的祖先链上
        if entry.entry_id == first_kept_id and index < compaction_index:
            first_kept_index = index
            break

    if first_kept_index is None:
        return None

    messages: list[AgentMessage] = [
        UserMessage(content=[TextContent(text=f"[compaction]\n{summary}")]),
    ]
    messages.extend(_project_message_entries(entries[first_kept_index:]))
    return messages


def _project_message_entries(entries: list[SessionEntry]) -> list[AgentMessage]:
    messages: list[AgentMessage] = []
    for entry in entries:
        if entry.entry_type != ENTRY_TYPE_MESSAGE:
            continue
        try:
            messages.append(agent_message_from_dict(entry.payload))
        except (TypeError, ValueError, KeyError):
            # 损坏或未知 role 的 message payload 跳过，避免阻断组装
            continue
    return messages
