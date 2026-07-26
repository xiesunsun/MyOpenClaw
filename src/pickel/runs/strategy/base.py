from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from pickel.context.hook_feedback import HookFeedback
from pickel.conversations.agent_message import AssistantMessage
from pickel.conversations.session import Session

if TYPE_CHECKING:
    from pickel.runs.event_bus import EventBus
    from pickel.runs.run import Run


class ExecutionStrategy(ABC):
    """Agent 执行策略基类。"""

    @abstractmethod
    async def execute(
        self,
        run: Run,
        session: Session,
        bus: "EventBus | None" = None,
        turn_id: str | None = None,
        initial_hook_feedback: list[HookFeedback] | None = None,
    ) -> AssistantMessage:
        """推进 turn 内 step 循环，返回最终 AssistantMessage。

        turn_id 为 None 时自生成；由 Run.turn 传入可让 turn 级事件
        与 step 事件共享同一个 id（Task 5）。
        """
        raise NotImplementedError
