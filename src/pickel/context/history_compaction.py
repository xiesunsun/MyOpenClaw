"""HistoryCompaction 的选择、生成和 ConversationNode 提交。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Sequence

from pickel.context.model_context import ModelContext, SystemContent
from pickel.context.projection import ConversationProjector
from pickel.context.window import group_message_units
from pickel.conversations.agent_message import AgentMessage, UserMessage
from pickel.conversations.conversation_node import ConversationNode


@dataclass(frozen=True)
class HistoryCompactionPlan:
    first_kept_node_id: str
    messages: tuple[AgentMessage, ...] = ()
    details: dict[str, Any] | None = None


def _message_size(message: AgentMessage) -> int:
    value = ModelContext(SystemContent(), (message,)).to_dict()["messages"][0]
    return len(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    )


def plan_history_compaction_for_budget(
    nodes: Sequence[ConversationNode], *, target_token_budget: int
) -> HistoryCompactionPlan | None:
    """按预算保留尾部完整消息单元，返回待摘要消息和首个保留 Node。

    该选择器只使用可复现的 JSON 字节上界，不按固定轮数裁剪。已有压缩摘要会
    作为本轮待摘要输入的一部分；原始 ConversationNode 永远不被删除或改写。
    """
    if target_token_budget < 1:
        raise ValueError("target_token_budget 必须大于 0")
    if not nodes:
        return None

    latest_compaction = max(
        (
            index
            for index, node in enumerate(nodes)
            if node.content_type == "history_compaction"
        ),
        default=-1,
    )
    raw_nodes = [
        node
        for node in nodes[latest_compaction + 1 :]
        if node.content_type == "agent_message"
    ]
    if not raw_nodes:
        return None

    projected = ConversationProjector().project_conversation_messages(nodes)
    raw_messages = [node.content for node in raw_nodes]
    units = group_message_units(raw_messages)
    unit_starts: list[int] = []
    for index, message in enumerate(raw_messages):
        if index == 0 or isinstance(message, UserMessage):
            unit_starts.append(index)
    unit_nodes = [raw_nodes[index] for index in unit_starts]

    kept_units: list[list[AgentMessage]] = []
    used = 0
    for unit in reversed(units):
        cost = sum(_message_size(message) for message in unit)
        if kept_units and used + cost > target_token_budget:
            break
        # 至少保留一个完整单元；若它本身超过预算，二次 preflight 会给出
        # 稳定的 no-progress 错误，而不会把消息拆开或静默截断。
        kept_units.append(unit)
        used += cost
    kept_units.reverse()
    kept_count = len(kept_units)
    if kept_count >= len(units):
        return None

    first_kept_node = unit_nodes[len(units) - kept_count]
    kept_message_start = sum(len(unit) for unit in units[: len(units) - kept_count])
    projected_raw_start = max(0, len(projected) - len(raw_nodes))
    dropped_end = projected_raw_start + kept_message_start
    return HistoryCompactionPlan(
        first_kept_node_id=first_kept_node.node_id,
        messages=tuple(projected[:dropped_end]),
        details={
            "target_token_budget": target_token_budget,
            "kept_units": kept_count,
            "dropped_units": len(units) - kept_count,
        },
    )
