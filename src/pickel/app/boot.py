from datetime import timedelta
from pathlib import Path

from pickel.agents.agent import Agent
from pickel.agents.behavior_loader import BehaviorLoader
from pickel.agents.skills import SkillRegistry
from pickel.conversations.service import SessionService
from pickel.config.app_config import AppConfig
from pickel.config.paths import sessions_db_path
from pickel.context import (
    NoopSessionRecallProvider,
    SessionRecallProvider,
)
from pickel.context.assembler import ContextAssembler
from pickel.integrations.openviking.bypass_store import OpenVikingBypassStore
from pickel.integrations.openviking.commit_policy import ThresholdCommitPolicy
from pickel.integrations.openviking.context_client import SyncHTTPOpenVikingContextClient
from pickel.integrations.openviking.recall_adapter import OpenVikingRecall
from pickel.integrations.openviking.session_recall import OpenVikingSessionRecallProvider
from pickel.integrations.openviking.session_client import SyncHTTPOpenVikingSessionClient
from pickel.integrations.openviking.session_message_mapper import SessionMessageMapper
from pickel.integrations.openviking.session_sync import (
    NoopSessionSync,
    OpenVikingSessionSync,
    SessionSync,
)
from pickel.persistence.sqlite_session_repository import SQLiteSessionRepository
from pickel.shared.file_access import FileAccessMode
from pickel.tools.bus import ToolBus
from pickel.tools.catalog import install_builtin_tools
from pickel.runs import ReActStrategy
from pickel.runs.run import Run


class Boot:
    """Composition root：读配置，解析 Agent，构造 Run / SessionService。"""

    def __init__(self, app_config: AppConfig, tool_bus: ToolBus | None = None) -> None:
        self.app_config = app_config
        # bus 是进程级的：未注入时自建一个并装上内置工具
        if tool_bus is None:
            tool_bus = ToolBus()
            install_builtin_tools(tool_bus)
        self.tool_bus = tool_bus

    @classmethod
    def from_config(
        cls,
        app_config: AppConfig,
        tool_bus: ToolBus | None = None,
    ) -> "Boot":
        return cls(app_config, tool_bus=tool_bus)

    def resolve_agent(self, agent_id: str | None = None) -> Agent:
        resolved_agent_id = agent_id or self.app_config.default_agent
        agent_config = self.app_config.get_agent_config(resolved_agent_id)
        behavior_instruction = BehaviorLoader.load(agent_config.behavior_path)
        file_access_mode = self.app_config.resolve_file_access_mode(resolved_agent_id)
        skills_path = self._resolve_agent_skills_path(resolved_agent_id)
        # 初始列表；prepare 在 skills_path 非空时会每 turn re-discover
        skills = SkillRegistry.discover(skills_path)

        return Agent(
            agent_id=resolved_agent_id,
            workspace_path=agent_config.workspace_path,
            behavior_path=agent_config.behavior_path,
            behavior_instruction=behavior_instruction,
            model_config=self.app_config.resolve_model_config(agent_config.llm),
            tool_ids=list(agent_config.tools),
            file_access_mode=file_access_mode.value,
            skills=skills,
            skills_path=skills_path,
        )

    def _resolve_agent_skills_path(self, agent_id: str) -> Path | None:
        """解析并校验 skills 目录路径；不在此冻结 discover 结果。"""
        agent_config = self.app_config.get_agent_config(agent_id)
        skills_path = self.app_config.resolve_skills_path(agent_id)
        if skills_path is None:
            return None
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
        return skills_path

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
            tool_bus=self.tool_bus,
            strategy=ReActStrategy(max_steps=self.app_config.react_max_steps),
            session_service=session_service,
            context_assembler=ContextAssembler(),
            unit_window=self.app_config.context_cli_turn_window,
            recall_sources=self._build_recall_sources(agent_id=agent.agent_id),
        )
        return agent, run

    def _build_recall_sources(
        self,
        *,
        agent_id: str | None = None,
    ) -> list:
        """OV session recall 开启时挂 OpenVikingRecall；否则空列表。"""
        openviking_config = self.app_config.openviking
        if (
            openviking_config is None
            or not openviking_config.enabled
            or not openviking_config.session_recall.enabled
        ):
            return []
        provider = self._build_session_recall_provider(agent_id=agent_id)
        if isinstance(provider, NoopSessionRecallProvider):
            return []
        return [
            OpenVikingRecall(
                provider=provider,
                max_chars=openviking_config.session_recall.max_chars,
            )
        ]

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
