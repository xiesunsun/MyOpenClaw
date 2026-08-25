"""会话历史压缩计划与提交。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from pickel.context.projection import ConversationProjector
from pickel.context.window import group_conversation_turns
from pickel.conversations.conversation_node import ConversationNode, HistoryCompaction
from pickel.conversations.conversation_service import ConversationService


@dataclass(frozen=True)
class HistoryCompactionPlan:
    summary: str
    first_kept_node_id: str
    details: dict[str, Any] | None = None


def plan_history_compaction(
    nodes: Sequence[ConversationNode], *, keep_turns: int, summary: str
) -> HistoryCompactionPlan | None:
    """保留最近的完整用户轮次，返回可持久化的 typed 计划。"""
    if keep_turns <= 0:
        raise ValueError("keep_turns 必须大于 0")
    if not nodes:
        return None
    # 已经存在压缩事实时，摘要本身没有对应的 ConversationNode 轮次；
    # 本次只规划尚未压缩的固定 leaf，避免把合成摘要错配到真实 Node。
    if any(node.content_type == "history_compaction" for node in nodes):
        return None

    messages = ConversationProjector().project_conversation_messages(nodes)
    message_nodes = [node for node in nodes if node.content_type == "agent_message"]

    turns = group_conversation_turns(messages)
    if len(turns) <= keep_turns:
        return None
    dropped_turns = len(turns) - keep_turns
    first_kept_message_index = sum(len(turn) for turn in turns[:dropped_turns])
    if first_kept_message_index >= len(message_nodes):
        return None
    return HistoryCompactionPlan(
        summary=summary,
        first_kept_node_id=message_nodes[first_kept_message_index].node_id,
        details={"keep_turns": keep_turns, "dropped_turns": dropped_turns},
    )


def commit_history_compaction(
    service: ConversationService,
    *,
    session_id: str,
    plan: HistoryCompactionPlan,
) -> ConversationNode:
    """经 ConversationService 的唯一 Node/CAS 写边界提交压缩事实。"""
    return service.append_history_compaction(
        session_id=session_id,
        content=HistoryCompaction(
            summary=plan.summary,
            first_kept_node_id=plan.first_kept_node_id,
        ),
    )
