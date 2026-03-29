from myopenclaw.agent.definition import AgentDefinition
from myopenclaw.app.behavior_loader import BehaviorLoader
from myopenclaw.config.app_config import AppConfig


class AppAssembly:
    def __init__(self, app_config: AppConfig) -> None:
        self.app_config = app_config

    def resolve_agent_definition(self, agent_id: str | None = None) -> AgentDefinition:
        resolved_agent_id = agent_id or self.app_config.default_agent
        agent_config = self.app_config.get_agent_config(resolved_agent_id)
        behavior_instruction = BehaviorLoader.load(agent_config.behavior_path)
        return AgentDefinition(
            agent_id=resolved_agent_id,
            workspace_path=agent_config.workspace_path,
            behavior_path=agent_config.behavior_path,
            behavior_instruction=behavior_instruction,
            model_config=self.app_config.resolve_model_config(agent_config.llm),
            tool_ids=list(agent_config.tools),
        )
