from pathlib import Path

from myopenclaw.agents.agent import Agent
from myopenclaw.agents.behavior_loader import BehaviorLoader
from myopenclaw.config.app_config import AppConfig
from myopenclaw.runs import AgentCoordinator, AgentRuntimeContext, ReActStrategy


class AppAssembly:
    """Composition root for application objects."""

    def __init__(self, app_config: AppConfig) -> None:
        self.app_config = app_config

    @classmethod
    def from_config_path(cls, config_path: Path) -> "AppAssembly":
        return cls(AppConfig.load(config_path))

    def resolve_agent(self, agent_id: str | None = None) -> Agent:
        resolved_agent_id = agent_id or self.app_config.default_agent
        agent_config = self.app_config.get_agent_config(resolved_agent_id)
        behavior_instruction = BehaviorLoader.load(agent_config.behavior_path)

        return Agent(
            agent_id=resolved_agent_id,
            workspace_path=agent_config.workspace_path,
            behavior_path=agent_config.behavior_path,
            behavior_instruction=behavior_instruction,
            model_config=self.app_config.resolve_model_config(agent_config.llm),
            tool_ids=list(agent_config.tools),
            file_access_mode=self.app_config.resolve_file_access_mode(
                resolved_agent_id
            ).value,
        )

    def build_chat_runtime(
        self,
        agent_id: str | None = None,
    ) -> tuple[Agent, AgentCoordinator]:
        agent = self.resolve_agent(agent_id=agent_id)
        coordinator = AgentCoordinator(
            strategy=ReActStrategy(max_steps=self.app_config.react_max_steps),
            context=AgentRuntimeContext.create(agent=agent),
        )
        return agent, coordinator
