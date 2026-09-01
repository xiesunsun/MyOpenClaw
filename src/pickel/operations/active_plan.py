"""Operation 当前活动计划值对象。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal

PlanItemStatus = Literal["pending", "in_progress", "completed"]
_STATUSES = {"pending", "in_progress", "completed"}


@dataclass(frozen=True)
class PlanItem:
    step: str
    status: PlanItemStatus

    def __post_init__(self) -> None:
        if not isinstance(self.step, str):
            raise TypeError("计划 step 必须是字符串")
        normalized = self.step.strip()
        if not normalized:
            raise ValueError("计划 step 不能为空")
        if len(normalized) > 500:
            raise ValueError("计划 step 不能超过 500 个字符")
        if self.status not in _STATUSES:
            raise ValueError(f"不支持的计划项状态: {self.status!r}")
        object.__setattr__(self, "step", normalized)


@dataclass(frozen=True)
class ActivePlan:
    items: tuple[PlanItem, ...]

    def __post_init__(self) -> None:
        items = tuple(self.items)
        if not 1 <= len(items) <= 20:
            raise ValueError("活动计划必须包含 1 到 20 个计划项")
        if any(not isinstance(item, PlanItem) for item in items):
            raise TypeError("活动计划 items 必须全部是 PlanItem")
        if sum(item.status == "in_progress" for item in items) > 1:
            raise ValueError("活动计划最多只能有一个 in_progress 项")
        if all(item.status == "completed" for item in items):
            raise ValueError("全部完成的计划不应构造为 ActivePlan")
        object.__setattr__(self, "items", items)

    def to_dict(self) -> dict[str, Any]:
        return {
            "items": [{"step": item.step, "status": item.status} for item in self.items]
        }

    def to_json(self) -> str:
        return json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )

    @classmethod
    def from_json(cls, value: str) -> "ActivePlan":
        try:
            data = json.loads(value)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("ActivePlan 必须是合法 JSON object") from exc
        parsed = parse_active_plan(data)
        if parsed is None:
            raise ValueError("全部完成的计划不能恢复为 ActivePlan")
        return parsed


def parse_active_plan(value: Any) -> ActivePlan | None:
    """严格解析 update_plan 或数据库中的计划；全完成计划返回 None。"""
    if not isinstance(value, dict) or set(value) != {"items"}:
        raise ValueError("ActivePlan 必须是只包含 items 的 JSON object")
    items_value = value["items"]
    if not isinstance(items_value, list):
        raise TypeError("ActivePlan.items 必须是 JSON array")
    if not 1 <= len(items_value) <= 20:
        raise ValueError("活动计划必须包含 1 到 20 个计划项")
    items: list[PlanItem] = []
    for item in items_value:
        if not isinstance(item, dict) or set(item) != {"step", "status"}:
            raise ValueError("计划项必须只包含 step 和 status")
        items.append(PlanItem(step=item["step"], status=item["status"]))
    if all(item.status == "completed" for item in items):
        return None
    return ActivePlan(tuple(items))


def active_plan_from_content(value: dict[str, Any]) -> ActivePlan | None:
    return parse_active_plan(value)


def active_plan_from_json(value: str) -> ActivePlan | None:
    try:
        data = json.loads(value)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("ActivePlan 必须是合法 JSON object") from exc
    return parse_active_plan(data)


def active_plan_to_dict(value: ActivePlan | None) -> dict[str, Any] | None:
    return value.to_dict() if value is not None else None


def render_active_plan(plan: ActivePlan) -> str:
    """将计划渲染为 ModelContext 尾部使用的临时 Markdown 消息。"""
    marks = {"completed": "x", "in_progress": "~", "pending": " "}
    lines = ["<active_plan>", "", "# Work Plan", ""]
    lines.extend(f"- [{marks[item.status]}] {item.step}" for item in plan.items)
    lines.extend(["", "</active_plan>"])
    return "\n".join(lines)
