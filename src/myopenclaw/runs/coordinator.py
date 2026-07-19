"""AgentCoordinator：turn 边界 + user checkpoint。"""

from __future__ import annotations

from dataclasses import dataclass

from myopenclaw.agents.agent import Agent
from myopenclaw.conversations.agent_message import AssistantMessage, UserMessage
from myopenclaw.conversations.content_blocks import TextContent
from myopenclaw.conversations.session import Session
from myopenclaw.runs.dependencies import RunDependencies
from myopenclaw.runs.strategy.base import ExecutionStrategy, RuntimeEventHandler


@dataclass
class AgentCoordinator:
    """协调一次用户 query 的执行。"""

    strategy: ExecutionStrategy
    deps: RunDependencies | None = None

    async def run_turn(
        self,
        *,
        agent: Agent,
        session: Session,
        user_text: str,
        event_handler: RuntimeEventHandler | None = None,
        deps: RunDependencies | None = None,
    ) -> AssistantMessage:
        if session.agent_id != agent.agent_id:
            raise ValueError(
                f"Session '{session.session_id}' belongs to agent '{session.agent_id}', "
                f"not '{agent.agent_id}'"
            )

        run_deps = deps or self.deps
        if run_deps is None:
            raise ValueError("RunDependencies 未提供")
        if run_deps.agent.agent_id != agent.agent_id:
            # 允许外部传入与 agent 一致的 deps
            raise ValueError("RunDependencies.agent 与参数 agent 不一致")

        user_entry = session.append_user(
            UserMessage(content=[TextContent(text=user_text)])
        )
        if run_deps.session_service is not None:
            run_deps.session_service.flush_new_entries(
                session=session,
                entries=[user_entry],
            )

        return await self.strategy.execute(
            deps=run_deps,
            session=session,
            event_handler=event_handler,
        )
