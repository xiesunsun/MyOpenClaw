"""会话历史压缩计划与提交。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from pickel.context.projection import ConversationProjector
from pickel.context.window import group_message_units
from pickel.conversations.conversation_node import ConversationEntry
from pickel.conversations.conversation_service import ConversationService


@dataclass(frozen=True)
class HistoryCompactionPlan:
    summary: str
    first_kept_node_id: str
    details: dict[str, Any] | None = None


def build_history_compaction_content(
    plan: HistoryCompactionPlan,
) -> dict[str, Any]:
    content: dict[str, Any] = {
        "summary": plan.summary,
        "first_kept_node_id": plan.first_kept_node_id,
    }
    if plan.details:
        content["details"] = dict(plan.details)
    return content


def plan_history_compaction(
    entries: Sequence[ConversationEntry],
    *,
    keep_units: int,
    summary: str,
) -> HistoryCompactionPlan | None:
    """保留最近的消息单元，返回可持久化的压缩计划。"""
    if keep_units <= 0:
        raise ValueError("keep_units 必须大于 0")
    if not entries:
        return None

    messages = ConversationProjector().project_conversation_messages(entries)
    message_entries = [
        entry for entry in entries if entry.object.object_type == "agent_message"
    ]
    if len(message_entries) != len(messages):
        # 损坏消息会被 Projector 跳过，此时保守地按持久化节点尾部保留。
        if len(message_entries) <= keep_units:
            return None
        first_kept = message_entries[-keep_units]
        return HistoryCompactionPlan(
            summary=summary,
            first_kept_node_id=first_kept.node.node_id,
        )

    units = group_message_units(messages)
    if len(units) <= keep_units:
        return None
    dropped_units = len(units) - keep_units
    first_kept_message_index = sum(len(unit) for unit in units[:dropped_units])
    if first_kept_message_index >= len(message_entries):
        return None
    return HistoryCompactionPlan(
        summary=summary,
        first_kept_node_id=(message_entries[first_kept_message_index].node.node_id),
        details={
            "keep_units": keep_units,
            "dropped_units": dropped_units,
        },
    )


def commit_history_compaction(
    service: ConversationService,
    *,
    session_id: str,
    plan: HistoryCompactionPlan,
) -> ConversationEntry:
    """经 ConversationService 的原子写边界提交压缩事实。"""
    return service.append_history_compaction(
        session_id=session_id,
        content=build_history_compaction_content(plan),
    )
