from abc import ABC, abstractmethod
from typing import Callable

from myopenclaw.conversations.message import SessionMessage
from myopenclaw.conversations.session import Session
from myopenclaw.runs.events import RuntimeEvent
from myopenclaw.shared.generation import GenerateResult
from myopenclaw.runs.context import AgentRuntimeContext


RuntimeEventHandler = Callable[[RuntimeEvent], None | object]


class ExecutionStrategy(ABC):
    """Base interface for agent execution strategies."""

    @abstractmethod
    async def execute(
        self,
        context: AgentRuntimeContext,
        session: Session,
        session_recall_message: SessionMessage | None = None,
        event_handler: RuntimeEventHandler | None = None,
    ) -> GenerateResult:
        """Executes the strategy flow to produce a generation result."""
        pass
