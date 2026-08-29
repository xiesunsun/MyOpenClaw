"""Agent 的 Goal/Plan 协作状态与模型行为约束。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

CollaborationMode = Literal["normal", "plan", "goal"]

# Plan 模式只允许检查和读取。自定义工具默认不在集合中，避免工具没有声明
# 读写能力时被错误地当成只读工具。
PLAN_READ_ONLY_TOOL_NAMES = frozenset({"ls", "glob", "grep", "read"})


@dataclass(frozen=True)
class CollaborationState:
    """一次 Session 当前使用的协作模式快照。

    该值对象目前由 Host 进程持有；Operation 仍绑定自己的 Package 和工作区。
    后续需要跨进程恢复 Goal 时，再把它作为 Session 的持久化字段迁移，不把
    临时模式偷偷塞进 ConversationNode。
    """

    mode: CollaborationMode = "normal"
    goal: str | None = None
    plan: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.mode not in {"normal", "plan", "goal"}:
            raise ValueError(f"不支持的协作模式: {self.mode!r}")
        if self.mode == "goal" and not self.goal:
            raise ValueError("Goal 模式必须提供 goal")
        if self.goal is not None and not self.goal.strip():
            raise ValueError("goal 不能是空白字符串")
        steps = tuple(step.strip() for step in self.plan if step.strip())
        object.__setattr__(self, "plan", steps)

    def system_prompt(self) -> str:
        """生成动态协作约束；真正的权限限制由 Runtime 另行执行。"""

        if self.mode == "plan":
            plan = "\n".join(
                f"{index}. {step}" for index, step in enumerate(self.plan, 1)
            )
            plan_text = plan or "尚未形成计划。先理解需求，再输出可执行计划。"
            return (
                "Plan mode is active.\n"
                "你当前只能检查和读取，不能修改文件、运行有副作用的命令或提交变更。\n"
                "先完成：Initial Understanding、Design、Review；最后只输出计划，"
                "不要开始执行计划。\n"
                f"当前计划：\n{plan_text}"
            )
        if self.mode == "goal":
            assert self.goal is not None
            return (
                "Goal mode is active.\n"
                f"目标：{self.goal}\n"
                "持续工作直到有可验证证据表明目标完成；每一步都应说明证据、"
                "未完成项和下一步动作。不要把猜测当作完成。"
            )
        return ""


__all__ = [
    "CollaborationMode",
    "CollaborationState",
    "PLAN_READ_ONLY_TOOL_NAMES",
]
