from abc import ABC, abstractmethod
from typing import Callable

from myopenclaw.conversation.session import Session
from myopenclaw.runtime.events import RuntimeEvent
from myopenclaw.runtime.generation import GenerateResult
from myopenclaw.runtime.context import AgentRuntimeContext


RuntimeEventHandler = Callable[[RuntimeEvent], None | object]


class ExecutionStrategy(ABC):
    """Base interface for agent execution strategies."""

    @abstractmethod
    async def execute(
        self,
        context: AgentRuntimeContext,
        session: Session,
        event_handler: RuntimeEventHandler | None = None,
    ) -> GenerateResult:
        """Executes the strategy flow to produce a generation result."""
        pass
