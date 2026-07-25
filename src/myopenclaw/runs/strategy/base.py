from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Callable

from myopenclaw.context.hook_feedback import HookFeedback
from myopenclaw.conversations.agent_message import AssistantMessage
from myopenclaw.conversations.session import Session
from myopenclaw.runs.events import RuntimeEvent

if TYPE_CHECKING:
    from myopenclaw.runs.run import Run


RuntimeEventHandler = Callable[[RuntimeEvent], None | object]


class ExecutionStrategy(ABC):
    """Agent 执行策略基类。"""

    @abstractmethod
    async def execute(
        self,
        run: Run,
        session: Session,
        event_handler: RuntimeEventHandler | None = None,
        initial_hook_feedback: list[HookFeedback] | None = None,
    ) -> AssistantMessage:
        """推进 turn 内 step 循环，返回最终 AssistantMessage。"""
        raise NotImplementedError
