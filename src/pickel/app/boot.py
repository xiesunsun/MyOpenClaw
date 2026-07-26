from pathlib import Path

from pickel.agents.agent import Agent
from pickel.agents.behavior_loader import BehaviorLoader
from pickel.agents.skills import SkillRegistry
from pickel.conversations.service import SessionService
from pickel.config.app_config import AppConfig
from pickel.config.paths import home_dir, sessions_db_path
from pickel.context.assembler import ContextAssembler
from pickel.conversations.session_sync import CompositeSessionSync
from pickel.extensions_host.registry import AgentScope, ExtensionRegistry
from pickel.hooks.lifecycle import LifecycleHooks
from pickel.persistence.sqlite_session_repository import SQLiteSessionRepository
from pickel.shared.file_access import FileAccessMode
from pickel.tools.bus import ToolBus
from pickel.tools.sandbox import SandboxPolicy
from pickel.tools.shell import ShellSessionManager
from pickel.tools.catalog import install_builtin_tools
from pickel.runs import ReActStrategy
from pickel.runs.run import Run


class Boot:
    """Composition root：读配置，解析 Agent，构造 Run / SessionService。"""

    def __init__(
        self,
        app_config: AppConfig,
        tool_bus: ToolBus | None = None,
        extensions: ExtensionRegistry | None = None,
    ) -> None:
        self.app_config = app_config
        # bus 是进程级的：未注入时自建一个并装上内置工具
        if tool_bus is None:
            tool_bus = ToolBus()
            install_builtin_tools(tool_bus)
        self.tool_bus = tool_bus
        self.extensions = extensions or ExtensionRegistry()
        # CLI 装载入口回填 LoadResult，供 ChatLoop 在 /reload 时 teardown 旧 extension
        self.extension_result = None
        self._sandbox_policy: SandboxPolicy | None = None

    @property
    def sandbox_policy(self) -> SandboxPolicy:
        """进程级沙箱策略。shell 会话经它包裹 spawn（S2）。"""
        if self._sandbox_policy is None:
            self._sandbox_policy = SandboxPolicy.from_settings(
                self.app_config.sandbox,
                home=home_dir(),
                project_root=self.app_config.root,
            )
        return self._sandbox_policy

    @classmethod
    def from_config(
        cls,
        app_config: AppConfig,
        tool_bus: ToolBus | None = None,
        extensions: ExtensionRegistry | None = None,
    ) -> "Boot":
        return cls(app_config, tool_bus=tool_bus, extensions=extensions)

    # --- extension 贡献的按 agent 求值 ---

    def _scope(self, agent_id: str | None) -> AgentScope:
        resolved = agent_id or self.app_config.default_agent
        return AgentScope(agent_id=resolved, app_config=self.app_config)

    def resolve_recall_sources(self, agent_id: str | None = None) -> list:
        return self.extensions.recall_sources(self._scope(agent_id))

    def resolve_hook_handlers(self, agent_id: str | None = None) -> list:
        return self.extensions.hook_handlers(self._scope(agent_id))

    def resolve_session_syncs(self, agent_id: str | None = None) -> list:
        return self.extensions.session_syncs(self._scope(agent_id))

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
            recall_sources=self.resolve_recall_sources(agent.agent_id),
            lifecycle_hooks=LifecycleHooks(
                handlers=self.resolve_hook_handlers(agent.agent_id)
            ),
            shell_session_manager=ShellSessionManager(sandbox=self.sandbox_policy),
        )
        return agent, run

    def build_session_service(self, agent_id: str | None = None) -> SessionService:
        # 全局会话库：~/.pickel/sessions.db（或 PICKEL_HOME）
        db_path = sessions_db_path()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        repository = SQLiteSessionRepository(db_path)
        return SessionService(
            repository,
            CompositeSessionSync(self.resolve_session_syncs(agent_id)),
        )
