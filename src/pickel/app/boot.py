from pathlib import Path

from pickel.agents.agent import Agent
from pickel.agents.agent_package import LoadedAgentPackage
from pickel.agents.agent_package_builder import AgentPackageBuilder
from pickel.config.app_config import AppConfig
from pickel.config.paths import home_dir, sessions_db_path
from pickel.runs.legacy_model_context_builder import LegacyModelContextBuilder
from pickel.conversations.service import SessionService
from pickel.conversations.session_sync import CompositeSessionSync
from pickel.extensions_host.registry import AgentScope, ExtensionRegistry
from pickel.hooks.lifecycle import LifecycleHooks
from pickel.persistence.sqlite_session_repository import SQLiteSessionRepository
from pickel.runs import ReActStrategy
from pickel.runs.run import Run
from pickel.skills.store import SkillStore
from pickel.tools.bus import ToolBus
from pickel.tools.catalog import install_builtin_tools
from pickel.tools.sandbox import SandboxPolicy
from pickel.tools.shell import LocalBashOperations


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
        self._agent_package_builder = AgentPackageBuilder(
            app_config=app_config,
            tool_bus=tool_bus,
        )
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
        return self.resolve_loaded_agent_package(agent_id).agent

    def resolve_loaded_agent_package(
        self,
        agent_id: str | None = None,
    ) -> LoadedAgentPackage:
        return self._agent_package_builder.build_loaded_agent_package(agent_id)

    def _resolve_agent_skills_path(self, agent_id: str) -> Path | None:
        """复用 AgentPackageBuilder 的 Pickel 设置路径解析。"""
        return self._agent_package_builder.resolve_skills_path(agent_id)

    def build_run(
        self,
        agent_id: str | None = None,
        session_service: SessionService | None = None,
    ) -> tuple[Agent, Run]:
        agent = self.resolve_agent(agent_id=agent_id)
        run = Run.open(
            skill_store=self._build_skill_store(agent.agent_id),
            agent=agent,
            tool_bus=self.tool_bus,
            strategy=ReActStrategy(max_steps=self.app_config.react_max_steps),
            session_service=session_service,
            model_context_builder=LegacyModelContextBuilder(),
            unit_window=self.app_config.context_cli_turn_window,
            recall_sources=self.resolve_recall_sources(agent.agent_id),
            lifecycle_hooks=LifecycleHooks(
                handlers=self.resolve_hook_handlers(agent.agent_id)
            ),
            bash_operations=LocalBashOperations(sandbox=self.sandbox_policy),
        )
        return agent, run

    def _build_skill_store(self, agent_id: str) -> SkillStore | None:
        """没有 skills 目录的 agent 拿不到 store —— skill_manage 会据此报错。"""
        skills_path = self._resolve_agent_skills_path(agent_id)
        if skills_path is None:
            return None
        return SkillStore(
            skills_path=skills_path,
            pending_dir=home_dir() / "pending" / "skills",
            write_approval=self.app_config.skills.write_approval,
            guard=self.app_config.skills.guard,
            agent_id=agent_id,
        )

    def build_session_service(self, agent_id: str | None = None) -> SessionService:
        # 全局会话库：~/.pickel/sessions.db（或 PICKEL_HOME）
        db_path = sessions_db_path()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        repository = SQLiteSessionRepository(db_path)
        return SessionService(
            repository,
            CompositeSessionSync(self.resolve_session_syncs(agent_id)),
        )
