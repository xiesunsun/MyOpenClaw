"""进程级 Runtime 组合与活动 Conversation 管理。"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from pickel.app.boot import Boot
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
from pickel.conversations.conversation_session import ConversationSession
from pickel.extensions_host.event_processor import ConversationExtensionContext
from pickel.extensions_host.loader import (
    LoadResult,
    load_extensions_async,
    teardown_extensions,
)
from pickel.persistence.in_memory_runtime_store import InMemoryRuntimeStore
from pickel.persistence.runtime_store import RuntimeStore
from pickel.shared.conversation_mode import ConversationMode


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
        self._extension_result = getattr(boot, "extension_result", None) or LoadResult()

    @property
    def boot(self) -> Boot:
        return self._boot

    @property
    def app_config(self) -> AppConfig:
        return self._boot.app_config

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
        source = self._boot.extensions.mcp_status_source
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
        if request.persistence == "ephemeral":
            store: RuntimeStore = InMemoryRuntimeStore()
        else:
            store = self._boot.runtime_store()
        service = self._boot.build_conversation_service(store=store)
        if request.session_id is not None:
            session = service.load_conversation_session(request.session_id)
            agent_id = session.agent_id
        else:
            loaded = self._boot.resolve_loaded_agent_package(request.agent_id)
            agent_id = loaded.version.agent_id
            session = service.create_conversation_session(
                agent_id=agent_id,
                cwd=str((request.cwd or Path.cwd()).resolve()),
            )
        return self._attach(
            boot=self._boot,
            store=store,
            service=service,
            session=session,
            agent_id=agent_id,
            persistence=request.persistence,
            mode=request.mode,
        )

    def new_session(self, conversation: ConversationRuntime) -> ConversationRuntime:
        next_conversation = self.open_conversation(
            ConversationRequest(
                agent_id=conversation.agent.agent_id,
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
        """重载 Definition，并为后续 AgentRun 捕获新的 Package Version。"""
        app_config = app_config or Config.load(cwd=Path.cwd())
        tool_bus = self._boot.tool_bus
        await teardown_extensions(self._extension_result, tool_bus=tool_bus)
        extension_result = await load_extensions_async(
            tool_bus=tool_bus,
            app_config=app_config,
            enabled_names=app_config.resolve_agent_extensions(self._launch_agent_ids),
        )
        next_boot = boot_factory(
            app_config,
            tool_bus=tool_bus,
            extensions=extension_result.registry,
        )
        next_boot.extension_result = extension_result
        store = (
            conversation.runtime_store
            if conversation.persistence == "ephemeral"
            else next_boot.runtime_store()
        )
        next_conversation = self._attach(
            boot=next_boot,
            store=store,
            service=next_boot.build_conversation_service(store=store),
            session=conversation.session,
            agent_id=conversation.agent.agent_id,
            persistence=conversation.persistence,
            mode=conversation.mode,
        )
        conversation.detach()
        self._boot = next_boot
        self._extension_result = extension_result
        return ReloadResult(
            conversation=next_conversation,
            warnings=tuple(str(item) for item in extension_result.errors),
        )

    def _attach(
        self,
        *,
        boot: Boot,
        store: RuntimeStore,
        service: ConversationService,
        session: ConversationSession,
        agent_id: str,
        persistence: str,
        mode: ConversationMode,
    ) -> ConversationRuntime:
        loaded, agent_runtime = boot.build_agent_runtime(
            agent_id,
            store=(store if persistence == "ephemeral" else None),
        )
        conversation = ConversationRuntime(
            loaded_agent_package=loaded,
            agent_runtime=agent_runtime,
            session=session,
            conversation_service=service,
            runtime_store=store,
            persistence=persistence,
            app_config=boot.app_config,
            mode=mode,
        )
        self._add_event_processors(conversation, registry=boot.extensions)
        return conversation

    @staticmethod
    def _add_event_processors(
        conversation: ConversationRuntime,
        *,
        registry: Any,
    ) -> None:
        context = ConversationExtensionContext(
            agent_id=conversation.agent.agent_id,
            session_id=conversation.session.session_id,
            mode=conversation.mode,
            publish_output=conversation.publish_output,
            start_background_task=conversation.start_background_task,
        )
        for resolved in registry.resolve_event_processors(context):
            conversation.add_event_processor(resolved.processor, resolved.event_types)

    async def shutdown(self) -> None:
        await teardown_extensions(self._extension_result, tool_bus=self._boot.tool_bus)


def _implementation_label(name: str | None, version: str | None) -> str | None:
    if name and version:
        return f"{name} {version}"
    return name or version
