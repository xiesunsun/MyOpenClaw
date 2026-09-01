"""维护当前 Operation 工作计划的纯内置工具。"""

from __future__ import annotations

from typing import Any

from pickel.tools.base import ToolExecutionContext, ToolExecutionError, tool

UPDATE_PLAN_INPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "explanation": {
            "type": "string",
            "description": "可选。说明为什么创建或重写计划。",
        },
        "plan": {
            "type": "array",
            "minItems": 1,
            "maxItems": 20,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "step": {"type": "string", "minLength": 1, "maxLength": 500},
                    "status": {
                        "type": "string",
                        "enum": ["pending", "in_progress", "completed"],
                    },
                },
                "required": ["step", "status"],
            },
        },
    },
    "required": ["plan"],
}

UPDATE_PLAN_OUTPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "updated": {"type": "boolean"},
        "active": {"type": "boolean"},
        "item_count": {"type": "integer", "minimum": 0},
        "completed_count": {"type": "integer", "minimum": 0},
    },
    "required": ["updated", "active", "item_count", "completed_count"],
}

_STATUSES = frozenset({"pending", "in_progress", "completed"})


@tool(
    name="update_plan",
    description=(
        "Create or replace the complete work plan for the current task. "
        "Use this for complex, ambiguous, or multi-step work; submit every "
        "plan item on each update."
    ),
    input_schema=UPDATE_PLAN_INPUT_SCHEMA,
    output_schema=UPDATE_PLAN_OUTPUT_SCHEMA,
    replay_policy="safe",
)
async def update_plan(
    arguments: dict[str, Any], context: ToolExecutionContext
) -> dict[str, object]:
    """校验完整计划并返回紧凑结果；状态更新由 OperationDriver 原子提交。"""
    del context
    plan = arguments.get("plan")
    if not isinstance(plan, list) or not 1 <= len(plan) <= 20:
        raise ToolExecutionError("update_plan 的 plan 必须包含 1 到 20 项。")

    in_progress_count = 0
    completed_count = 0
    for item in plan:
        if not isinstance(item, dict) or set(item) != {"step", "status"}:
            raise ToolExecutionError("update_plan 的每一项必须只有 step 和 status。")
        step = item["step"]
        status = item["status"]
        if not isinstance(step, str) or not step.strip() or len(step.strip()) > 500:
            raise ToolExecutionError(
                "update_plan 的 step 去除空白后必须为 1 到 500 字符。"
            )
        if status not in _STATUSES:
            raise ToolExecutionError(
                "update_plan 的 status 必须是 pending、in_progress 或 completed。"
            )
        if status == "in_progress":
            in_progress_count += 1
        elif status == "completed":
            completed_count += 1

    if in_progress_count > 1:
        raise ToolExecutionError("update_plan 最多允许一个 in_progress 步骤。")

    active = completed_count != len(plan)
    return {
        "updated": True,
        "active": active,
        "item_count": len(plan),
        "completed_count": completed_count,
    }
