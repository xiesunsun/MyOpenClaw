"""Compaction 策略：在单元边界写入 compaction entry。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from myopenclaw.context.projection import project_messages
from myopenclaw.context.window import group_message_units
from myopenclaw.conversations.session import Session
from myopenclaw.conversations.session_entry import SessionEntry


@dataclass(frozen=True)
class CompactionPlan:
    summary: str
    first_kept_entry_id: str
    details: dict[str, Any] | None = None


def build_compaction_payload(plan: CompactionPlan) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "payload_version": 1,
        "summary": plan.summary,
        "first_kept_entry_id": plan.first_kept_entry_id,
    }
    if plan.details:
        payload["details"] = dict(plan.details)
    return payload


def plan_keep_last_units(
    session: Session,
    *,
    keep_units: int,
    summary: str,
) -> CompactionPlan | None:
    """保留最近 keep_units 个不可拆分单元；返回 compaction 计划。"""
    path = session.active_path()
    if not path:
        return None
    messages = project_messages(path)
    # 需要 message entry 与 AgentMessage 对齐：仅 message 类型
    message_entries = [e for e in path if e.entry_type == "message"]
    if len(message_entries) != len(messages):
        # 保守：按 entry 尾部保留
        if len(message_entries) <= keep_units:
            return None
        first_kept = message_entries[-keep_units]
        return CompactionPlan(summary=summary, first_kept_entry_id=first_kept.entry_id)

    units = group_message_units(messages)
    if len(units) <= keep_units:
        return None
    # 计算保留单元对应的第一条 message 在 messages 中的下标
    drop_units = len(units) - keep_units
    msg_index = sum(len(u) for u in units[:drop_units])
    if msg_index >= len(message_entries):
        return None
    first_kept = message_entries[msg_index]
    return CompactionPlan(
        summary=summary,
        first_kept_entry_id=first_kept.entry_id,
        details={"keep_units": keep_units, "dropped_units": drop_units},
    )


def apply_compaction(session: Session, plan: CompactionPlan) -> SessionEntry:
    return session.append_compaction(build_compaction_payload(plan))
