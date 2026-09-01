"""Goal 模式的独立完成验证协议。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class GoalVerification:
    passed: bool
    reason: str
    next_action: str


def parse_goal_verification(text: str) -> GoalVerification:
    """解析 worker 的严格 JSON；任何格式问题都按未完成处理。"""

    try:
        value: Any = json.loads(text)
    except (TypeError, json.JSONDecodeError):
        return GoalVerification(
            passed=False,
            reason="Goal verifier 返回了非法 JSON",
            next_action="重新检查目标并给出可验证证据",
        )
    if not isinstance(value, dict):
        return GoalVerification(False, "Goal verifier 返回的不是对象", "重新检查目标")
    passed = value.get("passed")
    reason = value.get("reason")
    next_action = value.get("nextAction")
    if (
        not isinstance(passed, bool)
        or not isinstance(reason, str)
        or not isinstance(next_action, str)
    ):
        return GoalVerification(
            False,
            "Goal verifier 缺少 passed/reason/nextAction 字段",
            "重新检查目标并给出可验证证据",
        )
    return GoalVerification(passed, reason.strip(), next_action.strip())


def build_goal_verification_prompt(goal: str, candidate: str) -> str:
    return (
        "验证下面的代码 Agent 是否已经完成目标。只能依据候选结果中的事实判断，"
        "不能调用工具，不能补充猜测。仅输出 JSON，不要 Markdown：\n"
        '{"passed": boolean, "reason": string, "nextAction": string}\n\n'
        f"目标：{goal}\n\n候选结果：\n{candidate}"
    )


__all__ = [
    "GoalVerification",
    "build_goal_verification_prompt",
    "parse_goal_verification",
]
