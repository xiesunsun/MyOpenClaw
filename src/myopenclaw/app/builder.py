from pathlib import Path

from myopenclaw.agent.agent import Agent
from myopenclaw.app.behavior_loader import BehaviorLoader
from myopenclaw.config.app_config import AppConfig


class AgentBuilder:
    """Factory to build Agent instances from configurations."""

    @staticmethod
    def build_from_config(config_path: Path, agent_id: str | None = None) -> Agent:
        """Loads the configuration from file and builds the Agent."""
        app_config = AppConfig.load(config_path)
        return AgentBuilder.build_from_app_config(app_config, agent_id=agent_id)

    @staticmethod
    def build_from_app_config(
        app_config: AppConfig, agent_id: str | None = None
    ) -> Agent:
        """Builds an Agent from an existing AppConfig instance."""
        resolved_agent_id = agent_id or app_config.default_agent
        agent_config = app_config.get_agent_config(resolved_agent_id)
        behavior_instruction = BehaviorLoader.load(agent_config.behavior_path)

        return Agent(
            agent_id=resolved_agent_id,
            workspace_path=agent_config.workspace_path,
            behavior_path=agent_config.behavior_path,
            behavior_instruction=behavior_instruction,
            model_config=app_config.resolve_model_config(agent_config.llm),
            tool_ids=list(agent_config.tools),
        )
