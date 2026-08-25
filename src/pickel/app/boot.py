"""Pickel 的组合根。

Boot 只负责把配置解析成 LoadedAgentPackage，并把窄领域端口接到 Agent
和 Driver。执行状态、Conversation 树和 Inbox 事实仍由各自的 Service/Store
负责；这里不再创建 AgentRuntime、RuntimeBindings 或 RuntimeStore 资源袋。
"""

from __future__ import annotations

from pathlib import Path
import os
from typing import Any, Protocol

from pickel.agents.agent_package import (
    AgentPackageVersion,
    ExtensionVersion,
    LoadedAgentPackage,
    ModelVersion,
)
from pickel.agents.agent_package_builder import AgentPackageBuilder
from pickel.agents.agent_package_loader import AgentPackageLoader
from pickel.artifacts.artifact_service import ArtifactService
from pickel.artifacts.artifact_store import ArtifactStore
from pickel.artifacts.filesystem_blob_store import FilesystemBlobStore
from pickel.artifacts.in_memory_blob_store import InMemoryBlobStore
from pickel.config.app_config import AppConfig
from pickel.config.paths import artifact_blobs_path, home_dir, runtime_db_path
from pickel.conversations.conversation_service import ConversationService
from pickel.conversations.conversation_store import ConversationStore
from pickel.extensions_host.registry import AgentScope, ExtensionRegistry
from pickel.shared.frozen_json import thaw_json
from pickel.inbox.store import InboxStore
from pickel.hooks.lifecycle import LifecycleHooks, NoopLifecycleHooks
from pickel.operations.operation_service import OperationService
from pickel.operations.operation_store import OperationStore
from pickel.agents.agent_package_store import AgentPackageVersionStore
from pickel.persistence.in_memory_runtime_store import InMemoryRuntimeStore
from pickel.persistence.sqlite_runtime_store import SQLiteRuntimeStore
from pickel.providers.anthropic import AnthropicProvider
from pickel.runtime.agent_driver import AgentDriver, build_agent_inbox
from pickel.runtime.agent import Agent
from pickel.runtime.operation_driver import OperationDriver
from pickel.runtime.runtime_effects import RuntimeEffects
from pickel.skills.store import SkillStore
from pickel.tools.base import ToolExecutionContext, ToolExecutionResult
from pickel.tools.bus import ToolActivation, ToolBus
from pickel.tools.catalog import install_builtin_tools
from pickel.tools.file_service import WorkspaceFileService
from pickel.tools.policy import FullAccessPathPolicy, WorkspacePathAccessPolicy
from pickel.tools.sandbox import SandboxPolicy
from pickel.tools.services import ToolServices
from pickel.tools.shell import LocalBashOperations
from pickel.workspaces.workspace_binding import WorkspaceBinding


class CompositionStore(
    ConversationStore,
    InboxStore,
    OperationStore,
    AgentPackageVersionStore,
    ArtifactStore,
    Protocol,
):
    """Boot 所需的窄端口交集；不提供通用资源袋方法。"""


class Boot:
    """从 AppConfig 装配一代可执行 Agent。"""

    def __init__(
        self,
        app_config: AppConfig,
        tool_bus: ToolBus | None = None,
        extensions: ExtensionRegistry | None = None,
    ) -> None:
        self.app_config = app_config
        if tool_bus is None:
            tool_bus = ToolBus()
            install_builtin_tools(tool_bus)
        self.tool_bus = tool_bus
        self.extensions = extensions or ExtensionRegistry()
        self._agent_package_builder = AgentPackageBuilder(
            app_config=app_config,
            tool_bus=tool_bus,
            extension_versions=self.extensions.extension_versions,
        )
        self.extension_result = None
        self._sandbox_policy: SandboxPolicy | None = None
        self._runtime_store: SQLiteRuntimeStore | None = None

    @property
    def sandbox_policy(self) -> SandboxPolicy:
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

    def _scope(self, agent_id: str | None) -> AgentScope:
        return AgentScope(
            agent_id=agent_id or self.app_config.default_agent,
            app_config=self.app_config,
        )

    def resolve_recall_sources(self, agent_id: str | None = None) -> list[Any]:
        return self.extensions.recall_sources(self._scope(agent_id))

    def resolve_hook_handlers(self, agent_id: str | None = None) -> list[Any]:
        return self.extensions.hook_handlers(self._scope(agent_id))

    def resolve_loaded_agent_package(
        self,
        agent_id: str | None = None,
        *,
        store: CompositionStore | None = None,
    ) -> LoadedAgentPackage:
        """先冻结 Package，再解析当前 Generation 的可执行实现。"""
        version = self._agent_package_builder.build_agent_package_version(agent_id)
        resolved_id = agent_id or self.app_config.default_agent
        model_config = self.app_config.resolve_model_config(
            self.app_config.get_agent_config(resolved_id).llm
        )
        if version.model_policy.primary.provider != "anthropic":
            raise ValueError(
                "当前 LoadedAgentPackage 只支持 Anthropic Provider: "
                f"{version.model_policy.primary.provider}"
            )
        artifact_service = self._artifact_service(store)
        provider = AnthropicProvider.from_config(
            model_config,
            artifact_service=artifact_service,
        )
        snapshot = self.tool_bus.snapshot(
            ToolActivation(
                allowed=frozenset(self.app_config.get_agent_config(resolved_id).tools)
            )
        )
        if {item.name for item in version.tools} != set(snapshot.names):
            raise ValueError("ToolSnapshot 与 AgentPackageVersion 不一致")
        hooks, recalls = self._extension_contributions(
            version.extensions,
            agent_id=resolved_id,
        )
        return LoadedAgentPackage(
            version=version,
            model_clients={"primary": provider},
            tool_snapshot=snapshot,
            lifecycle_hooks=hooks,
            recall_sources=recalls,
        )

    def load_agent_package(
        self,
        package_version_id: str,
        *,
        store: CompositionStore,
        expected_agent_id: str | None = None,
    ) -> LoadedAgentPackage:
        """按 Operation 绑定的 Package Version 精确恢复。

        这里绝不调用 ``AgentPackageBuilder``。Builder 只服务于新 Session；恢复
        必须从 Store 读取冻结内容，并按其中的实现引用逐一校验当前 Generation。
        """
        artifact_service = self._artifact_service(store)

        def load_provider(model: ModelVersion) -> Any | None:
            config = self._model_config_from_version(model)
            if model.provider == "anthropic":
                return AnthropicProvider.from_config(
                    config, artifact_service=artifact_service
                )
            # 当前核心只承诺 Anthropic；未来 Provider 必须通过显式实现注册表接入。
            provider = self.extensions.providers(self._scope(expected_agent_id)).get(
                model.provider
            )
            return provider

        resolved_agent_id = expected_agent_id or self.app_config.default_agent

        def load_extension(extension: ExtensionVersion):
            self._require_extension_secrets(extension)
            return self.extensions.resolve_extension_contributions(
                extension,
                self._scope(resolved_agent_id),
            )

        return AgentPackageLoader(
            store,
            self.tool_bus,
            provider_loader=load_provider,
            extension_loader=load_extension,
        ).load(package_version_id, expected_agent_id=expected_agent_id)

    def _extension_contributions(
        self,
        extensions: tuple[ExtensionVersion, ...],
        *,
        agent_id: str,
    ) -> tuple[tuple[Any, ...], tuple[Any, ...]]:
        hooks: list[Any] = []
        recalls: list[Any] = []
        scope = self._scope(agent_id)
        for extension in extensions:
            self._require_extension_secrets(extension)
            extension_hooks, extension_recalls = (
                self.extensions.resolve_extension_contributions(extension, scope)
            )
            hooks.extend(extension_hooks)
            recalls.extend(extension_recalls)
        return tuple(hooks), tuple(recalls)

    def _require_extension_secrets(self, extension: ExtensionVersion) -> None:
        missing = [
            ref.name
            for ref in extension.required_secret_refs
            if self._extension_secret_value(ref.name) is None
        ]
        if missing:
            raise ValueError(f"缺少 Extension 所需 SecretRef: {', '.join(missing)}")

    def _extension_secret_value(self, name: str) -> Any | None:
        parts = name.split(".")
        if len(parts) < 3 or parts[0] != "extensions":
            if len(parts) == 2 and parts[0] == "environ":
                return os.environ.get(parts[1])
            return None
        value: Any = self.app_config.extensions.get(parts[1])
        for part in parts[2:]:
            if isinstance(value, dict):
                value = value.get(part)
            elif isinstance(value, list) and part.isdigit():
                index = int(part)
                value = value[index] if index < len(value) else None
            else:
                return None
        return value

    def build_agent(
        self,
        *,
        store: CompositionStore,
        session_id: str,
        loaded_agent_package: LoadedAgentPackage,
        session_cwd: Path,
        operation_service: OperationService | None = None,
    ) -> Agent:
        """装配一个 Agent；持久化依赖仅通过窄 Store port 传入。"""
        conversation_store = store
        inbox_store = store
        operation_service = operation_service or OperationService(store)
        effects = self._build_effects(
            store=store,
            loaded_agent_package=loaded_agent_package,
            session_cwd=session_cwd,
        )
        package_id = loaded_agent_package.version.package_version_id
        loaded_packages = {package_id: loaded_agent_package}

        def loaded_for(requested: str) -> LoadedAgentPackage:
            cached = loaded_packages.get(requested)
            if cached is None:
                cached = self.load_agent_package(
                    requested,
                    store=store,
                    expected_agent_id=loaded_agent_package.version.agent_id,
                )
                loaded_packages[requested] = cached
            return cached

        operation_driver = OperationDriver(
            operation_service=operation_service,
            conversation_service=ConversationService(conversation_store),
            package_loader=lambda requested: loaded_for(requested).version,
            effects_resolver=lambda requested: self._build_effects(
                store=store,
                loaded_agent_package=loaded_for(requested),
                session_cwd=session_cwd,
            ),
        )
        agent_driver = AgentDriver(
            conversation_store=conversation_store,
            inbox_store=inbox_store,
            operation_service=operation_service,
            operation_driver=operation_driver,
            package_resolver=lambda session: (
                package_id,
                WorkspaceBinding(
                    workspace_id=session.workspace_id,
                    working_directory=session.cwd,
                    allowed_root=(
                        session.cwd
                        if loaded_agent_package.version.workspace_policy.file_scope
                        == "workspace"
                        else None
                    ),
                ),
            ),
            cancel_operation=operation_service.request_cancellation,
        )
        return Agent(
            session_id=session_id,
            inbox=build_agent_inbox(session_id=session_id, store=inbox_store),
            driver=agent_driver,
        )

    def build_conversation_service(
        self, *, store: CompositionStore
    ) -> ConversationService:
        return ConversationService(store)

    def runtime_store(self) -> SQLiteRuntimeStore:
        """返回进程共享的具体 SQLite 适配器，而不是通用资源袋。"""
        if self._runtime_store is None:
            path = runtime_db_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            self._runtime_store = SQLiteRuntimeStore(path)
        return self._runtime_store

    def _artifact_service(self, store: CompositionStore | None) -> ArtifactService:
        artifact_store = store or self.runtime_store()
        return ArtifactService(
            artifact_store=artifact_store,
            blob_store=(
                InMemoryBlobStore()
                if isinstance(artifact_store, InMemoryRuntimeStore)
                else FilesystemBlobStore(artifact_blobs_path())
            ),
        )

    def _build_effects(
        self,
        *,
        store: CompositionStore,
        loaded_agent_package: LoadedAgentPackage,
        session_cwd: Path,
    ) -> RuntimeEffects:
        agent_id = loaded_agent_package.version.agent_id
        provider = loaded_agent_package.model_clients["primary"]
        artifact_service = provider.artifact_service
        workspace_root = Path(session_cwd).resolve()
        services = ToolServices(
            workspace_files=WorkspaceFileService(
                workspace_root=workspace_root,
                access_policy=(
                    FullAccessPathPolicy()
                    if loaded_agent_package.version.workspace_policy.file_scope
                    == "full"
                    else WorkspacePathAccessPolicy()
                ),
            ),
            bash=LocalBashOperations(sandbox=self.sandbox_policy),
            skill_store=self._build_skill_store(agent_id),
            artifact_service=artifact_service,
        )

        # Hook 实现属于已加载 Package 的冻结执行贡献。恢复已有 Operation 时，
        # 这里只能使用 LoadedAgentPackage.lifecycle_hooks，不能重新从当前
        # Generation 的 ExtensionRegistry 求值。
        lifecycle_hooks = (
            LifecycleHooks(list(loaded_agent_package.lifecycle_hooks))
            if loaded_agent_package.lifecycle_hooks
            else NoopLifecycleHooks()
        )

        async def invoke_hook(hook_name: str, event: Any) -> Any:
            hook = getattr(lifecycle_hooks, hook_name, None)
            if hook is None:
                return None
            return await hook(event)

        async def execute_tool(*, operation, state, tool_call_id: str, host_calls=None):
            call = next(
                item
                for item in state.current_step.tool_calls
                if item.tool_call_id == tool_call_id
            )
            entry = loaded_agent_package.tool_snapshot.get(call.tool_name)
            if entry is None:
                return ToolExecutionResult(
                    content=f"工具不可用: {call.tool_name}", is_error=True
                )
            context = ToolExecutionContext(
                agent_id=agent_id,
                session_id=operation.session_id,
                workspace_path=workspace_root,
                services=ToolServices(
                    workspace_files=services.workspace_files,
                    bash=services.bash,
                    skill_store=services.skill_store,
                    host_calls=host_calls,
                    artifact_service=artifact_service,
                ),
                operation_id=operation.operation_id,
                step_id=state.current_step.step_id,
                step_sequence=state.current_step.step_sequence,
                tool_call_id=tool_call_id,
            )
            try:
                return await entry.tool.execute(dict(call.arguments), context)
            except Exception as exc:
                return ToolExecutionResult(
                    content=f"工具 '{call.tool_name}' 执行失败: {exc}",
                    is_error=True,
                )

        return RuntimeEffects(
            provider=provider,
            execute_tool=execute_tool,
            invoke_hook_effect=invoke_hook,
            recall_sources=tuple(loaded_agent_package.recall_sources),
            provider_name=loaded_agent_package.version.model_policy.primary.provider,
            model_name=loaded_agent_package.version.model_policy.primary.model,
        )

    @staticmethod
    def _load_version(
        package_id: str, current: AgentPackageVersion
    ) -> AgentPackageVersion:
        if package_id != current.package_version_id:
            raise ValueError(f"当前 Generation 无法装载 Package: {package_id}")
        return current

    @staticmethod
    def _load_effects(
        package_id: str, current_id: str, effects: RuntimeEffects
    ) -> RuntimeEffects:
        if package_id != current_id:
            raise ValueError(f"当前 Generation 无法装载 Package: {package_id}")
        return effects

    def _build_skill_store(self, agent_id: str) -> SkillStore | None:
        skills_path = self._agent_package_builder.resolve_skills_path(agent_id)
        if skills_path is None:
            return None
        return SkillStore(
            skills_path=skills_path,
            pending_dir=home_dir() / "pending" / "skills",
            write_approval=self.app_config.skills.write_approval,
            guard=self.app_config.skills.guard,
            agent_id=agent_id,
        )

    def _model_config_from_version(self, model: ModelVersion) -> Any:
        """用冻结模型参数 + 当前 SecretRef 值重建 Provider 配置。"""
        options = dict(thaw_json(model.provider_options))
        api_key: str | None = None
        for ref in model.required_secret_refs:
            if ref.name == f"providers.{model.provider}.api_key":
                api_key = self._secret_value(ref.name, model)
            elif ref.name.startswith(f"providers.{model.provider}.options."):
                value = self._secret_value(ref.name, model)
                if value is not None:
                    cursor = options
                    option_path = ref.name.split(".")[3:]
                    for key in option_path[:-1]:
                        child = cursor.get(key)
                        if not isinstance(child, dict):
                            child = {}
                            cursor[key] = child
                        cursor = child
                    cursor[option_path[-1]] = value
        missing = [
            ref.name
            for ref in model.required_secret_refs
            if self._secret_value(ref.name, model) is None
        ]
        if missing:
            raise ValueError(f"缺少 Package 所需 SecretRef: {', '.join(missing)}")
        from pickel.shared.model_config import ModelConfig

        return ModelConfig(
            provider=model.provider,
            model=model.model,
            api_key=api_key,
            api_base=model.api_base,
            temperature=model.temperature,
            max_input_tokens=model.max_input_tokens,
            max_output_tokens=model.max_output_tokens,
            provider_options=options,
        )

    def _secret_value(self, name: str, model: ModelVersion) -> str | None:
        parts = name.split(".")
        if parts[:2] == ["providers", model.provider]:
            auth = self.app_config.auth_providers.get(model.provider) or {}
            if parts[2:] == ["api_key"] and auth.get("api_key") is not None:
                return str(auth["api_key"])
            if len(parts) >= 4 and parts[2] == "options":
                catalog = self.app_config.providers.get(model.provider)
                entry = catalog.models.get(model.model) if catalog else None
                value: Any = entry.provider_options if entry else None
                for key in parts[3:]:
                    if not isinstance(value, dict):
                        value = None
                        break
                    value = value.get(key)
                return str(value) if value is not None else None
            if parts[2:] == ["api_key"]:
                catalog = self.app_config.providers.get(model.provider)
                entry = catalog.models.get(model.model) if catalog else None
                value = entry.api_key if entry else None
                return str(value) if value is not None else None
        if parts[:1] == ["environ"] and len(parts) == 2:
            return os.environ.get(parts[1])
        return None
