from dataclasses import dataclass

from myopenclaw.agents.agent import Agent
from myopenclaw.context import build_session_recall_message
from myopenclaw.conversations.session import Session
from myopenclaw.runs.context import AgentRuntimeContext
from myopenclaw.shared.generation import GenerateResult
from myopenclaw.runs.strategy.base import ExecutionStrategy, RuntimeEventHandler


@dataclass
class AgentCoordinator:
    """Coordinates agent execution using a given strategy."""

    strategy: ExecutionStrategy
    context: AgentRuntimeContext | None = None

    async def run_turn(
        self,
        *,
        agent: Agent,
        session: Session,
        user_text: str,
        event_handler: RuntimeEventHandler | None = None,
    ) -> GenerateResult:
        """Runs a single conversation turn."""
        if session.agent_id != agent.agent_id:
            raise ValueError(
                f"Session '{session.session_id}' belongs to agent '{session.agent_id}', "
                f"not '{agent.agent_id}'"
            )

        # Initialize context once if not already done, caching the heavy resolvers
        if self.context is None or self.context.agent.agent_id != agent.agent_id:
            self.context = AgentRuntimeContext.create(agent=agent)

        session.append_user_message(user_text)
        session_recall_result = await self.context.session_recall_provider.recall(
            session=session,
            current_user_text=user_text,
        )
        session_recall_message = build_session_recall_message(
            session_recall_result,
            max_chars=self.context.session_recall_max_chars,
        )
        self.context.last_session_recall_message = session_recall_message

        return await self.strategy.execute(
            context=self.context,
            session=session,
            session_recall_message=session_recall_message,
            event_handler=event_handler,
        )
