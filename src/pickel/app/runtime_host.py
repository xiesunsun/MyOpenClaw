"""进程级 Runtime 组合与活动 Conversation 管理。"""

from __future__ import annotations

from collections.abc import Callable
import logging
from pathlib import Path
from typing import Any
from uuid import uuid4

from pickel.agents.agent_package_loader import PackageLoadError
from pickel.app.boot import Boot, CompositionStore
from pickel.app.conversation_runtime import ConversationRuntime
from pickel.app.runtime_models import (
    AgentInfo,
    ConversationRequest,
    McpInspection,
    McpServerInfo,
    ModelInfo,
    ReloadResult,
)
from pickel.config.app_config import AppConfig
from pickel.config.loader import Config
from pickel.conversations.conversation_service import ConversationService
from pickel.conversations.agent_message import UserMessage
from pickel.conversations.conversation_session import ConversationSession
from pickel.extensions_host.event_processor import ConversationExtensionContext
from pickel.extensions_host.loader import (
    LoadResult,
    load_extensions_async,
    teardown_extensions,
)
from pickel.app.runtime_generation import (
    ExtensionInstance,
    LoadedPackageHandle,
    RuntimeGeneration,
)
from pickel.inbox.message import InboxMessage
from pickel.persistence.in_memory_runtime_store import InMemoryRuntimeStore
from pickel.operations.operation_service import OperationService
from pickel.operations.agent_delegation import AgentDelegation
from pickel.operations.delegation_service import DelegationService
from pickel.operations.agent_run_state import DelegateAgentIntent
from pickel.runtime.agent import Agent
from pickel.runtime.agent_registry import AgentRegistry
from pickel.shared.conversation_mode import ConversationMode
from pickel.tools.bus import ToolBus
from pickel.tools.catalog import install_builtin_tools

logger = logging.getLogger(__name__)


class _RuntimeDelegationControl:
    """把工具的 durable acceptance 接到当前 Host 的 child activation。"""

    def __init__(self, host: "RuntimeHost", store: CompositionStore) -> None:
        self._host = host
        self._store = store

    async def start_delegation(
        self,
        *,
        parent_operation_id: str,
        parent_step_id: str,
        parent_tool_call_id: str,
        message: UserMessage,
    ) -> AgentDelegation:
        delegation = DelegationService(store=self._store).start_delegation(
            parent_operation_id,
            parent_step_id,
            parent_tool_call_id,
            message,
        )
        try:
            await self._activate_child(delegation.child_session_id)
        except Exception:
            logger.exception(
                "Child Session 激活失败，将由下次启动恢复兜底: session_id=%s",
                delegation.child_session_id,
            )
        return delegation

    async def send_parent_followup(
        self,
        *,
        sender_operation_id: str,
        sender_step_id: str,
        sender_tool_call_id: str,
        target_child_session_id: str,
        message: UserMessage,
    ) -> InboxMessage:
        stored = DelegationService(store=self._store).send_parent_followup(
            sender_operation_id,
            sender_step_id,
            sender_tool_call_id,
            target_child_session_id,
            message,
        )
        try:
            await self._activate_child(target_child_session_id)
        except Exception:
            logger.exception(
                "Child Session 激活失败，将由下次启动恢复兜底: session_id=%s",
                target_child_session_id,
            )
        return stored

    async def _activate_child(self, session_id: str) -> None:
        await self._host.activate_agent(session_id, self._store)
        self._host.agent_registry.wake(session_id)


class RuntimeHost:
    """进程级入口；只负责组合和替换活动 Runtime。"""

    def __init__(
        self,
        boot: Boot,
        *,
        launch_agent_ids: tuple[str, ...] | None = None,
    ) -> None:
        self._boot = boot
        self._launch_agent_ids = launch_agent_ids
        self._extension_result = boot.extension_result or LoadResult()
        self._active_generation = self._build_generation(boot, self._extension_result)
        self._conversations: set[ConversationRuntime] = set()
        self._retired_generations: set[RuntimeGeneration] = set()
        self._agent_registry = AgentRegistry()
        self._headless_agents: dict[str, tuple[Agent, LoadedPackageHandle]] = {}

    @property
    def agent_registry(self) -> AgentRegistry:
        return self._agent_registry

    @classmethod
    async def create(
        cls,
        app_config: AppConfig,
        *,
        launch_agent_ids: tuple[str, ...] | None = None,
        boot_factory: Callable[..., Boot] = Boot.from_config,
    ) -> "RuntimeHost":
        """由 Host 统一拥有初始 Extension/Generation 的装配和失败清理。"""

        tool_bus = ToolBus()
        install_builtin_tools(tool_bus)
        result = await load_extensions_async(
            tool_bus=tool_bus,
            app_config=app_config,
            enabled_names=app_config.resolve_agent_extensions(launch_agent_ids),
        )
        try:
            boot = boot_factory(
                app_config,
                tool_bus=tool_bus,
                extensions=result.registry,
            )
        except BaseException:
            await teardown_extensions(result, tool_bus=tool_bus)
            raise
        boot.extension_result = result
        try:
            host = cls(boot, launch_agent_ids=launch_agent_ids)
            await host._recover_runnable_sessions()
            return host
        except BaseException:
            await teardown_extensions(result, tool_bus=tool_bus)
            raise

    @property
    def boot(self) -> Boot:
        return self._boot

    @property
    def app_config(self) -> AppConfig:
        return self._boot.app_config

    @property
    def load_result(self) -> LoadResult:
        return self._extension_result

    async def _recover_runnable_sessions(self) -> None:
        """启动时只恢复 Store 明确标出的可运行 Session。"""
        store = self._boot.runtime_store()
        for session_id in store.list_runnable_session_ids():
            try:
                await self.activate_agent(session_id, store)
            except Exception:
                logger.exception("启动恢复 Session 失败: session_id=%s", session_id)

    @property
    def active_generation(self) -> RuntimeGeneration:
        return self._active_generation

    async def activate_agent(self, session_id: str, store: CompositionStore) -> Agent:
        """为 durable Session 装配 headless Agent 并幂等唤醒。"""
        existing = self._headless_agents.get(session_id)
        if existing is not None:
            return existing[0]
        registered = self._agent_registry.get(session_id)
        if registered is not None:
            return registered
        session = store.load_session(session_id)
        if session is None:
            raise LookupError(f"ConversationSession 不存在: {session_id}")
        if session.archived_at is not None:
            raise ValueError("归档 Session 不能激活 Agent")
        boot = self._active_generation_boot()
        package_id: str | None = None
        if session.active_operation_id is not None:
            operation = store.load_operation(session.active_operation_id)
            if operation is None or operation.session_id != session_id:
                raise ValueError("Session.active_operation_id 指向无效 Operation")
            package_id = operation.agent_package_version_id
        else:
            delegation = store.load_delegation(session_id)
            if delegation is not None:
                parent_operation = store.load_operation(delegation.parent_operation_id)
                parent_state = store.load_run_state(delegation.parent_operation_id)
                if parent_operation is None or parent_state is None:
                    raise ValueError("Delegation parent Operation/State 不存在")
                step = parent_state.current_step
                call = (
                    next(
                        (
                            item
                            for item in step.tool_calls
                            if item.tool_call_id == delegation.parent_tool_call_id
                        ),
                        None,
                    )
                    if step is not None
                    else None
                )
                if (
                    step is None
                    or step.step_id != delegation.parent_step_id
                    or call is None
                    or not isinstance(call.execution_intent, DelegateAgentIntent)
                ):
                    raise ValueError(
                        "Delegation parent ToolCall 缺少 DelegateAgentIntent"
                    )
                package_id = call.execution_intent.child_package_version_id
        if package_id is None:
            loaded = boot.resolve_loaded_agent_package(session.agent_id, store=store)
        else:
            try:
                loaded = boot.load_agent_package(
                    package_id,
                    store=store,
                    expected_agent_id=session.agent_id,
                )
            except PackageLoadError:
                if session.active_operation_id is None:
                    raise
                # Agent 外壳可先使用当前包；OperationDriver 仍会按 Operation
                # 绑定的历史包精确加载，并把缺失记录为可恢复失败。
                loaded = boot.resolve_loaded_agent_package(
                    session.agent_id, store=store
                )
        if loaded.version.agent_id != session.agent_id:
            raise ValueError("LoadedAgentPackage.agent_id 与 Session 不匹配")
        generation = self._active_generation
        loaded = generation.cache_loaded_package(
            loaded.version.package_version_id, loaded
        )
        handle = generation.acquire_loaded_package(loaded.version.package_version_id)
        agent: Agent | None = None
        registered = False
        try:
            agent = boot.build_agent(
                store=store,
                session_id=session_id,
                loaded_agent_package=loaded,
                session_cwd=session.cwd,
                operation_service=OperationService(store),
                wake_callback=self._agent_registry.wake,
                delegation_control=_RuntimeDelegationControl(self, store),
            )
            self._agent_registry.register(agent)
            registered = True
            store.insert_agent_package_version(loaded.version)
            self._headless_agents[session_id] = (agent, handle)
            self._agent_registry.wake(session_id)
            return agent
        except BaseException:
            self._headless_agents.pop(session_id, None)
            if registered and agent is not None:
                self._agent_registry.unregister(session_id, agent)
            await handle.close()
            raise

    def list_agents(self) -> tuple[AgentInfo, ...]:
        return tuple(
            AgentInfo(agent_id=item) for item in sorted(self.app_config.agents)
        )

    def list_models(self) -> tuple[ModelInfo, ...]:
        return tuple(
            ModelInfo(provider=provider, model=model)
            for provider in sorted(self.app_config.providers)
            for model in sorted(self.app_config.providers[provider].models)
        )

    def inspect_mcp(self, conversation: ConversationRuntime) -> McpInspection:
        generation = conversation.runtime_generation or self._active_generation
        catalog = generation.extension_catalog
        source = catalog.mcp_status_source
        if source is None:
            return McpInspection(available=False)
        snapshot = source.snapshot()
        active_by_server: dict[str, int] = {}
        for tool in conversation.list_tools():
            if tool.source == "mcp" and tool.origin is not None:
                active_by_server[tool.origin] = active_by_server.get(tool.origin, 0) + 1
        return McpInspection(
            available=True,
            servers=tuple(
                McpServerInfo(
                    name=server.name,
                    status=server.status,
                    transport=server.transport,
                    config_scope=server.config_scope,
                    protocol_version=server.protocol_version,
                    implementation=_implementation_label(
                        server.implementation_name,
                        server.implementation_version,
                    ),
                    discovered_tools=server.discovered_tools,
                    active_tools=active_by_server.get(server.name, 0),
                    last_error=server.last_error,
                )
                for server in snapshot.servers
            ),
            diagnostics=snapshot.diagnostics,
        )

    def open_conversation(self, request: ConversationRequest) -> ConversationRuntime:
        if request.session_id is not None:
            existing = self._find_open_conversation(request.session_id)
            if existing is not None:
                return existing
        generation = self._active_generation
        boot = self._active_generation_boot()
        if request.persistence == "ephemeral":
            store: CompositionStore = InMemoryRuntimeStore()
        else:
            store = boot.runtime_store()
        service = boot.build_conversation_service(store=store)
        if request.session_id is not None:
            session = service.load_conversation_session(request.session_id)
            agent_id = session.agent_id
        else:
            loaded = boot.resolve_loaded_agent_package(request.agent_id, store=store)
            agent_id = loaded.version.agent_id
            session = service.create_conversation_session(
                agent_id=agent_id,
                cwd=str((request.cwd or Path.cwd()).resolve()),
            )
        return self._attach(
            boot=boot,
            generation=generation,
            store=store,
            service=service,
            session=session,
            agent_id=agent_id,
            persistence=request.persistence,
            mode=request.mode,
        )

    def _find_open_conversation(self, session_id: str) -> ConversationRuntime | None:
        return next(
            (
                conversation
                for conversation in self._conversations
                if conversation.session.session_id == session_id
                and not conversation.closed
            ),
            None,
        )

    def new_session(self, conversation: ConversationRuntime) -> ConversationRuntime:
        next_conversation = self.open_conversation(
            ConversationRequest(
                agent_id=conversation.agent_definition.agent_id,
                persistence=conversation.persistence,
                cwd=Path.cwd(),
                mode=conversation.mode,
            )
        )
        conversation.detach()
        return next_conversation

    def switch_agent(
        self,
        conversation: ConversationRuntime,
        agent_id: str,
    ) -> ConversationRuntime:
        if agent_id not in self.app_config.agents:
            raise KeyError(f"Unknown agent: {agent_id}")
        next_conversation = self.open_conversation(
            ConversationRequest(
                agent_id=agent_id,
                persistence=conversation.persistence,
                cwd=Path.cwd(),
                mode=conversation.mode,
            )
        )
        conversation.detach()
        return next_conversation

    async def reload(
        self,
        conversation: ConversationRuntime,
        *,
        app_config: AppConfig | None = None,
        boot_factory: Callable[..., Boot] = Boot.from_config,
    ) -> ReloadResult:
        """构建新 Generation 后原子替换活动代；失败时旧代继续服务。"""
        app_config = app_config or Config.load(cwd=Path.cwd())
        tool_bus = ToolBus()
        install_builtin_tools(tool_bus)
        extension_result = await load_extensions_async(
            tool_bus=tool_bus,
            app_config=app_config,
            enabled_names=app_config.resolve_agent_extensions(self._launch_agent_ids),
        )
        try:
            next_boot = boot_factory(
                app_config,
                tool_bus=tool_bus,
                extensions=extension_result.registry,
            )
            next_boot.extension_result = extension_result
            next_generation = self._build_generation(next_boot, extension_result)
            store = (
                conversation.persistence_store
                if conversation.persistence == "ephemeral"
                else next_boot.runtime_store()
            )
            next_conversation = self._attach(
                boot=next_boot,
                generation=next_generation,
                store=store,
                service=next_boot.build_conversation_service(store=store),
                session=conversation.session,
                agent_id=conversation.agent_definition.agent_id,
                persistence=conversation.persistence,
                mode=conversation.mode,
                replace_agent=True,
            )
        except BaseException:
            # 新代尚未发布，任何构建/attach 失败都只回滚新资源。
            next_generation = locals().get("next_generation")
            if next_generation is not None:
                next_generation.retire()
                await next_generation.close()
            else:
                await teardown_extensions(extension_result, tool_bus=tool_bus)
            raise

        old_generation = self._active_generation
        self._active_generation = next_generation
        self._boot = next_boot
        self._extension_result = extension_result
        self._conversations.discard(conversation)
        self._conversations.add(next_conversation)
        old_generation.retire()
        self._retired_generations.add(old_generation)
        conversation.detach()
        # 有其他 Conversation/非终态 Operation 时旧代必须继续存活，reload
        # 不能被它们阻塞；无引用时等待已安排的清理，保证 reload 的短代完整收口。
        if old_generation.can_close:
            await old_generation.wait_closed()
            self._retired_generations.discard(old_generation)
        return ReloadResult(
            conversation=next_conversation,
            warnings=tuple(str(item) for item in extension_result.errors),
        )

    def _attach(
        self,
        *,
        boot: Boot,
        generation: RuntimeGeneration,
        store: CompositionStore,
        service: ConversationService,
        session: ConversationSession,
        agent_id: str,
        persistence: str,
        mode: ConversationMode,
        replace_agent: bool = False,
    ) -> ConversationRuntime:
        # 同一个 OperationService 同时服务 AgentDriver 与 UI Adapter，避免
        # ConversationRuntime 绕过窄服务直接读取 Operation 状态。
        operation_service = OperationService(store)
        if session.active_operation_id is not None:
            try:
                operation = operation_service.load_operation(
                    session.active_operation_id
                )
            except LookupError:
                raise ValueError(
                    "Session.active_operation_id 指向不存在的 Operation: "
                    f"{session.active_operation_id}"
                )
            if operation.session_id != session.session_id:
                raise ValueError("Operation 与 Session 不匹配")
            try:
                loaded = boot.load_agent_package(
                    operation.agent_package_version_id,
                    store=store,
                    expected_agent_id=agent_id,
                )
            except PackageLoadError:
                # Host 仍需要构建 Agent，由 OperationDriver 将同一装载
                # 失败以 revision CAS 持久化为 retryable failed。此处的
                # 当前 Package 只用于组合 UI/Host，不会执行旧 Operation。
                loaded = boot.resolve_loaded_agent_package(agent_id, store=store)
        else:
            loaded = boot.resolve_loaded_agent_package(agent_id, store=store)
        loaded = generation.cache_loaded_package(
            loaded.version.package_version_id,
            loaded,
        )
        package_handle = generation.acquire_loaded_package(
            loaded.version.package_version_id
        )
        previous_agent = self._agent_registry.get(session.session_id)
        try:
            store.insert_agent_package_version(loaded.version)
            if previous_agent is not None and not replace_agent:
                agent = previous_agent
            else:
                agent = boot.build_agent(
                    store=store,
                    session_id=session.session_id,
                    loaded_agent_package=loaded,
                    session_cwd=session.cwd,
                    operation_service=operation_service,
                    wake_callback=self._agent_registry.wake,
                    delegation_control=_RuntimeDelegationControl(self, store),
                )
            conversation = ConversationRuntime(
                loaded_agent_package=loaded,
                loaded_package_handle=package_handle,
                agent=agent,
                session=session,
                conversation_service=service,
                operation_service=operation_service,
                persistence_store=store,
                persistence=persistence,
                app_config=boot.app_config,
                mode=mode,
                on_detach=lambda: self._agent_registry.unregister(
                    session.session_id,
                    agent,
                ),
            )
        except BaseException:
            package_handle.close_sync()
            raise
        try:
            self._add_event_processors(conversation, registry=boot.extensions)
        except BaseException:
            conversation.detach()
            raise
        if previous_agent is None:
            try:
                self._agent_registry.register(agent)
            except BaseException:
                conversation.detach()
                raise
        elif replace_agent:
            self._agent_registry.unregister(session.session_id, previous_agent)
            try:
                self._agent_registry.register(agent)
            except BaseException:
                self._agent_registry.register(previous_agent)
                conversation.detach()
                raise
        self._conversations.add(conversation)
        return conversation

    @staticmethod
    def _add_event_processors(
        conversation: ConversationRuntime,
        *,
        registry: Any,
    ) -> None:
        context = ConversationExtensionContext(
            agent_id=conversation.agent_definition.agent_id,
            session_id=conversation.session.session_id,
            mode=conversation.mode,
            publish_output=conversation.publish_output,
            start_background_task=conversation.start_background_task,
        )
        for resolved in registry.resolve_event_processors(context):
            conversation.add_event_processor(resolved.processor, resolved.event_types)

    async def shutdown(self) -> None:
        await self._agent_registry.shutdown()
        for _session_id, (_agent, handle) in tuple(self._headless_agents.items()):
            await handle.close()
        self._headless_agents.clear()
        for conversation in tuple(self._conversations):
            conversation.detach()
        self._conversations.clear()
        generation = self._active_generation
        if not generation.closed and not generation.retired:
            generation.retire()
        generations = (generation, *tuple(self._retired_generations))
        for retired in generations:
            if retired.can_close:
                await retired.wait_closed()
        self._retired_generations = {
            retired for retired in self._retired_generations if not retired.closed
        }

    def _active_generation_boot(self) -> Boot:
        # RuntimeGeneration 的所有贡献属于随代切换的 Boot；这里单独方法让
        # Conversation/inspect 路径不会误读已经 retired 的 Boot。
        return self._boot

    @staticmethod
    def _build_generation(boot: Boot, result: LoadResult) -> RuntimeGeneration:
        generation = RuntimeGeneration(
            f"generation_{uuid4().hex}",
            extension_catalog=boot.extensions,
        )
        for extension_id, host in result.hosts.items():
            generation.add_extension(
                ExtensionInstance(
                    extension_id,
                    generation.generation_id,
                    host.scope,
                    extension_version=host.extension_version,
                )
            )
        generation.publish()
        return generation


def _implementation_label(name: str | None, version: str | None) -> str | None:
    if name and version:
        return f"{name} {version}"
    return name or version
