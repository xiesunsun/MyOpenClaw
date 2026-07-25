from datetime import timedelta
from pathlib import Path

from myopenclaw.agents.agent import Agent
from myopenclaw.agents.behavior_loader import BehaviorLoader
from myopenclaw.agents.skills import SkillManifest, SkillRegistry
from myopenclaw.conversations.service import SessionService
from myopenclaw.config.app_config import AppConfig
from myopenclaw.config.paths import sessions_db_path
from myopenclaw.context import (
    NoopSessionRecallProvider,
    SessionRecallProvider,
)
from myopenclaw.context.assembler import ContextAssembler
from myopenclaw.integrations.openviking.bypass_store import OpenVikingBypassStore
from myopenclaw.integrations.openviking.commit_policy import ThresholdCommitPolicy
from myopenclaw.integrations.openviking.context_client import SyncHTTPOpenVikingContextClient
from myopenclaw.integrations.openviking.session_recall import OpenVikingSessionRecallProvider
from myopenclaw.integrations.openviking.session_client import SyncHTTPOpenVikingSessionClient
from myopenclaw.integrations.openviking.session_message_mapper import SessionMessageMapper
from myopenclaw.integrations.openviking.session_sync import (
    NoopSessionSync,
    OpenVikingSessionSync,
    SessionSync,
)
from myopenclaw.persistence.sqlite_session_repository import SQLiteSessionRepository
from myopenclaw.shared.file_access import FileAccessMode
from myopenclaw.runs import ReActStrategy
from myopenclaw.runs.run import Run


class Boot:
    """Composition root：读配置，解析 Agent，构造 Run / SessionService。"""

    def __init__(self, app_config: AppConfig) -> None:
        self.app_config = app_config

    @classmethod
    def from_config_path(cls, config_path: Path) -> "Boot":
        return cls(AppConfig.load(config_path))

    @classmethod
    def from_config(cls, app_config: AppConfig) -> "Boot":
        return cls(app_config)

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

    def build_run(
        self,
        agent_id: str | None = None,
        session_service: SessionService | None = None,
    ) -> tuple[Agent, Run]:
        agent = self.resolve_agent(agent_id=agent_id)
        run = Run.open(
            agent=agent,
            strategy=ReActStrategy(max_steps=self.app_config.react_max_steps),
            session_service=session_service,
            context_assembler=ContextAssembler(),
            unit_window=self.app_config.context_cli_turn_window,
        )
        return agent, run

    def build_session_service(self, agent_id: str | None = None) -> SessionService:
        # 全局会话库：~/.pickel/sessions.db（或 PICKEL_HOME）
        db_path = sessions_db_path()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        repository = SQLiteSessionRepository(db_path)
        session_sync = self._build_session_sync(
            agent_id=agent_id,
            db_path=db_path,
        )
        return SessionService(repository, session_sync)

    def _build_session_sync(
        self,
        agent_id: str | None = None,
        *,
        db_path: Path | None = None,
    ) -> SessionSync:
        openviking_config = self.app_config.openviking
        if openviking_config is None or not openviking_config.enabled:
            return NoopSessionSync()
        remote_agent_id = self._resolve_openviking_remote_agent_id(agent_id=agent_id)
        if remote_agent_id is None:
            return NoopSessionSync()
        resolved_db = db_path or sessions_db_path()
        resolved_db.parent.mkdir(parents=True, exist_ok=True)
        return OpenVikingSessionSync(
            config=openviking_config,
            remote_agent_id=remote_agent_id,
            client=SyncHTTPOpenVikingSessionClient(
                openviking_config,
                remote_agent_id=remote_agent_id,
            ),
            message_mapper=SessionMessageMapper(
                tool_output_max_chars=openviking_config.tool_output_max_chars
            ),
            commit_policy=ThresholdCommitPolicy(
                commit_after=timedelta(minutes=openviking_config.commit_after_minutes),
                commit_after_turns=openviking_config.commit_after_turns,
            ),
            # OpenViking 游标旁路表，与 Session 核心解耦；与 sessions.db 同库
            state_store=OpenVikingBypassStore(resolved_db),
        )

    def _build_session_recall_provider(
        self,
        *,
        agent_id: str | None = None,
    ) -> SessionRecallProvider:
        openviking_config = self.app_config.openviking
        if (
            openviking_config is None
            or not openviking_config.enabled
            or not openviking_config.session_recall.enabled
        ):
            return NoopSessionRecallProvider()
        remote_agent_id = self._resolve_openviking_remote_agent_id(agent_id=agent_id)
        if remote_agent_id is None:
            return NoopSessionRecallProvider()
        db_path = sessions_db_path()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        return OpenVikingSessionRecallProvider(
            config=openviking_config,
            client=SyncHTTPOpenVikingContextClient(
                openviking_config,
                remote_agent_id=remote_agent_id,
            ),
            # 与 session_sync 共用同一旁路库，读取 remote_session_id
            state_store=OpenVikingBypassStore(db_path),
        )

    def _resolve_openviking_remote_agent_id(
        self,
        *,
        agent_id: str | None = None,
    ) -> str | None:
        openviking_config = self.app_config.openviking
        if openviking_config is None:
            return None
        resolved_agent_id = agent_id or self.app_config.default_agent
        remote_agent_config = openviking_config.agents.get(resolved_agent_id)
        if remote_agent_config is not None:
            if not remote_agent_config.enabled:
                return None
            return remote_agent_config.remote_agent_id
        remote_agent_id = self.app_config.get_agent_config(
            resolved_agent_id
        ).remote_agent_id
        if remote_agent_id is None:
            raise ValueError(
                f"OpenViking is enabled but no remote_agent_id is configured for agent '{resolved_agent_id}'"
            )
        return remote_agent_id
