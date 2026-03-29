from dataclasses import dataclass
from pathlib import Path

from myopenclaw.agent.agent import Agent
from myopenclaw.app.assembly import AppAssembly
from myopenclaw.config.app_config import AppConfig
from myopenclaw.tools.catalog import builtin_tools
from myopenclaw.tools.registry import ToolRegistry


@dataclass
class LoadedAgentRuntime:
    app_config: AppConfig
    agent_id: str
    agent: Agent


class AppBootstrap:
    @staticmethod
    def from_config_path(
        config_path: Path,
        agent_id: str | None = None,
    ) -> LoadedAgentRuntime:
        app_config = AppConfig.load(config_path)
        return AppBootstrap.from_app_config(app_config, agent_id=agent_id)

    @staticmethod
    def from_app_config(
        app_config: AppConfig,
        agent_id: str | None = None,
    ) -> LoadedAgentRuntime:
        definition = AppAssembly(app_config).resolve_agent_definition(agent_id=agent_id)
        tools = ToolRegistry(tools=builtin_tools()).resolve_many(definition.tool_ids)
        agent = Agent(definition=definition, tools=tools)
        return LoadedAgentRuntime(
            app_config=app_config,
            agent_id=definition.agent_id,
            agent=agent,
        )
