from pathlib import Path

from myopenclaw.application.services import ChatService
from myopenclaw.domain.agent import Agent
from myopenclaw.domain.session import Session
from myopenclaw.infrastructure.agents import BehaviorLoader
from myopenclaw.infrastructure.config import AppConfig
from myopenclaw.infrastructure.providers import GeminiAdapter
from myopenclaw.infrastructure.tools import BuiltinToolExecutor


class BootstrapAssembly:
    def __init__(self, app_config: AppConfig) -> None:
        self.app_config = app_config

    @classmethod
    def from_config_path(cls, config_path: Path) -> "BootstrapAssembly":
        return cls(AppConfig.load(config_path))

    def resolve_agent(self, agent_id: str | None = None) -> Agent:
        resolved_agent_id = agent_id or self.app_config.default_agent
        agent_config = self.app_config.get_agent_config(resolved_agent_id)
        behavior_instruction = BehaviorLoader.load(agent_config.behavior_path)
        llm_config = self.app_config.resolve_model_config(agent_config.llm)
        return Agent(
            agent_id=resolved_agent_id,
            workspace_path=agent_config.workspace_path,
            behavior_instruction=behavior_instruction,
            model_provider=llm_config.provider,
            model_name=llm_config.model,
            tool_ids=list(agent_config.tools),
            behavior_path=agent_config.behavior_path,
            file_access_mode=self.app_config.resolve_file_access_mode(
                resolved_agent_id
            ).value,
        )

    def build_chat_service(self, agent_id: str | None = None) -> ChatService:
        agent = self.resolve_agent(agent_id=agent_id)
        session = Session.create(agent_id=agent.agent_id)
        llm = GeminiAdapter.from_config(
            self.app_config.resolve_model_config(
                self.app_config.get_agent_config(agent.agent_id).llm
            )
        )
        tool_executor = BuiltinToolExecutor()
        return ChatService(
            agent=agent,
            session=session,
            llm=llm,
            tool_executor=tool_executor,
            max_steps=self.app_config.react_max_steps,
        )

    def build_chat_runtime(self, agent_id: str | None = None) -> tuple[Agent, ChatService]:
        service = self.build_chat_service(agent_id=agent_id)
        return service.agent, service
