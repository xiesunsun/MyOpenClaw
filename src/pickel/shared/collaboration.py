"""Agent 的 Goal 协作状态与模型行为约束。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

CollaborationMode = Literal["normal", "goal"]


@dataclass(frozen=True)
class CollaborationState:
    """一次 Session 当前使用的协作模式快照。

    该值对象目前由 Host 进程持有；Operation 仍绑定自己的 Package 和工作区。
    后续需要跨进程恢复 Goal 时，再把它作为 Session 的持久化字段迁移，不把
    临时模式偷偷塞进 ConversationNode。
    """

    mode: CollaborationMode = "normal"
    goal: str | None = None

    def __post_init__(self) -> None:
        if self.mode not in {"normal", "goal"}:
            raise ValueError(f"不支持的协作模式: {self.mode!r}")
        if self.mode == "goal" and not self.goal:
            raise ValueError("Goal 模式必须提供 goal")
        if self.goal is not None and not self.goal.strip():
            raise ValueError("goal 不能是空白字符串")

    def system_prompt(self) -> str:
        """生成动态协作约束；真正的权限限制由 Runtime 另行执行。"""

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
]
