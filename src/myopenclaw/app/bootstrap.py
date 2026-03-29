from dataclasses import dataclass
from pathlib import Path

from myopenclaw.agent.agent import Agent
from myopenclaw.app.assembly import AppAssembly
from myopenclaw.config.app_config import AppConfig


@dataclass
class LoadedAgent:
    app_config: AppConfig
    agent_id: str
    agent: Agent


class AppBootstrap:
    @staticmethod
    def from_config_path(
        config_path: Path,
        agent_id: str | None = None,
    ) -> LoadedAgent:
        app_config = AppConfig.load(config_path)
        return AppBootstrap.from_app_config(app_config, agent_id=agent_id)

    @staticmethod
    def from_app_config(
        app_config: AppConfig,
        agent_id: str | None = None,
    ) -> LoadedAgent:
        agent = AppAssembly(app_config).resolve_agent(agent_id=agent_id)
        return LoadedAgent(
            app_config=app_config,
            agent_id=agent.agent_id,
            agent=agent,
        )
