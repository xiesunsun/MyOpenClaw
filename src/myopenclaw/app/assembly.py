from pathlib import Path

from myopenclaw.agents.agent import Agent
from myopenclaw.agents.behavior_loader import BehaviorLoader
from myopenclaw.agents.skills import SkillManifest, SkillRegistry
from myopenclaw.config.app_config import AppConfig
from myopenclaw.context import ConversationContextService, ConversationWindowManager
from myopenclaw.shared.file_access import FileAccessMode
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
        file_access_mode = self.app_config.resolve_file_access_mode(resolved_agent_id)
        skills = self._resolve_agent_skills(resolved_agent_id)

        return Agent(
            agent_id=resolved_agent_id,
            workspace_path=agent_config.workspace_path,
            behavior_path=agent_config.behavior_path,
            behavior_instruction=behavior_instruction,
            model_config=self.app_config.resolve_model_config(agent_config.llm),
            tool_ids=list(agent_config.tools),
            file_access_mode=file_access_mode.value,
            skills=skills,
        )

    def _resolve_agent_skills(self, agent_id: str) -> list[SkillManifest]:
        agent_config = self.app_config.get_agent_config(agent_id)
        skills_path = self.app_config.resolve_skills_path(agent_id)
        if skills_path is None:
            return []
        if (
            skills_path.exists()
            and self.app_config.resolve_file_access_mode(agent_id)
            != FileAccessMode.FULL
            and not self._is_within_workspace(skills_path, agent_config.workspace_path)
        ):
            raise ValueError(
                f"Skills path '{skills_path}' is outside workspace '{agent_config.workspace_path}' "
                "and requires file_access_mode: full"
            )
        return SkillRegistry.discover(skills_path)

    @staticmethod
    def _is_within_workspace(path: Path, workspace_path: Path) -> bool:
        try:
            path.resolve().relative_to(workspace_path.resolve())
        except ValueError:
            return False
        return True

    def build_chat_runtime(
        self,
        agent_id: str | None = None,
    ) -> tuple[Agent, AgentCoordinator]:
        agent = self.resolve_agent(agent_id=agent_id)
        conversation_context_service = ConversationContextService(
            window_manager=ConversationWindowManager(
                cli_turn_window=self.app_config.context_cli_turn_window
            )
        )
        coordinator = AgentCoordinator(
            strategy=ReActStrategy(max_steps=self.app_config.react_max_steps),
            context=AgentRuntimeContext.create(
                agent=agent,
                conversation_context_service=conversation_context_service,
            ),
        )
        return agent, coordinator
