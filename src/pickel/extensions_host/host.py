"""给 extension 用的宿主 API。

ExtensionHost 是 per-extension 实例：loader 为每个 extension 单独构造一个，
绑定它的名字与它那段配置。extension 因此不需要（也无法）自报名字。
"""

from __future__ import annotations

from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from pickel.agents.agent_package import ExtensionVersion
from pickel.extensions_host.errors import ExtensionConfigError
from pickel.extensions_host.event_processor import ConversationProcessorFactory
from pickel.extensions_host.event_processor import EventProcessorRegistration
from pickel.extensions_host.mcp_status import McpStatusSource
from pickel.extensions_host.registry import (
    ContributionScope,
    Disposer,
    ExtensionDraft,
    ExtensionRegistry,
    Factory,
    ProviderFactory,
)
from pickel.tools.base import BaseTool
from pickel.tools.bus import ToolBus, ToolSource, qualified_name

T = TypeVar("T", bound=BaseModel)


class ExtensionHost:
    def __init__(
        self,
        *,
        name: str,
        config_section: dict[str, Any] | None,
        tool_bus: ToolBus,
        registry: ExtensionRegistry,
        app_config: Any = None,
        scope: ContributionScope | None = None,
        defer_publish: bool = False,
        extension_version: ExtensionVersion | None = None,
    ) -> None:
        self.name = name
        self.app_config = app_config
        self._config_section = config_section
        self._tool_bus = tool_bus
        self._registry = registry
        self._scope = scope or ContributionScope()
        self._draft = ExtensionDraft()
        self._published = False
        self._defer_publish = defer_publish
        self._extension_version = extension_version

    @property
    def scope(self) -> ContributionScope:
        return self._scope

    @property
    def extension_version(self) -> ExtensionVersion | None:
        """本次装载捕获的不可变 ExtensionVersion。"""
        return self._extension_version

    def config(self, model: type[T]) -> T | None:
        """按本 extension 的名字取配置段，用给定模型解析。

        段不存在 → None（extension 据此决定用默认值还是不启用）；
        段存在但校验失败 → ExtensionConfigError（由 loader 按装载失败隔离）。
        """
        if self._config_section is None:
            return None
        try:
            return model.model_validate(self._config_section)
        except ValidationError as exc:
            raise ExtensionConfigError(
                f"Invalid config for extension '{self.name}': {exc}"
            ) from exc

    def register_tool(self, tool: BaseTool, *, disposer: Disposer | None = None) -> str:
        """注册进程级工具。最终名为 ext__<extension>__<tool>。"""
        if self._published:
            lease = self._tool_bus.register_lease(
                tool,
                source=ToolSource.EXTENSION,
                origin=self.name,
            )
            self._scope.own(lease)
            qualified = lease.name
        else:
            self._draft.tools.append(tool)
            qualified = qualified_name(tool.spec.name, ToolSource.EXTENSION, self.name)
        if disposer is not None:
            self.add_disposer(disposer)
        if not self._published:
            self._maybe_publish()
        return qualified

    def register_mcp_tool(
        self,
        tool: BaseTool,
        *,
        server: str,
        disposer: Disposer | None = None,
    ) -> str:
        """注册 MCP 代理工具。最终名为 mcp__<server>__<tool>。

        与 register_tool 的 ext__ 前缀分开：MCP 工具跑在子进程里，
        执行位置与信任级别不同，名字上必须能区分（T1 设计）。
        """
        if self._published:
            lease = self._tool_bus.register_lease(
                tool,
                source=ToolSource.MCP,
                origin=server,
            )
            self._scope.own(lease)
            qualified = lease.name
        else:
            self._draft.mcp_tools.append((tool, server))
            qualified = qualified_name(tool.spec.name, ToolSource.MCP, server)
        if disposer is not None:
            self.add_disposer(disposer)
        if not self._published:
            self._maybe_publish()
        return qualified

    def register_mcp_status_source(self, source: McpStatusSource) -> None:
        """注册唯一的 MCP 只读状态源，供 Runtime Application 查询。"""
        if self._published:
            self._registry.register_status_source(source, scope=self._scope)
            return
        if self._draft.mcp_status_source is not None:
            raise ValueError("MCP status source is already registered")
        self._draft.mcp_status_source = source
        self._maybe_publish()

    def add_hook_handler(self, factory: Factory) -> None:
        """注册 hook handler 工厂：(AgentScope) -> handler | None。

        handler 只需实现感兴趣的方法（duck typing，见 hooks/lifecycle.py 的 _call）。
        """
        if self._published:
            self._registry.register_hook(
                factory, extension_id=self.name, scope=self._scope
            )
        else:
            self._draft.hook_factories.append(factory)
            self._maybe_publish()

    def add_event_processor(
        self,
        *,
        event_types: tuple[type[Any], ...],
        factory: ConversationProcessorFactory,
    ) -> None:
        """注册会话级 Runtime 事件处理器。"""
        if not event_types:
            raise ValueError("event_types 不能为空")
        registration = EventProcessorRegistration(
            extension_name=self.name,
            event_types=event_types,
            factory=factory,
        )
        if self._published:
            self._registry.register_event_processor(registration, scope=self._scope)
        else:
            self._draft.event_processors.append(registration)
            self._maybe_publish()

    def add_recall_source(self, factory: Factory) -> None:
        """注册召回源工厂：(AgentScope) -> Recall | None。"""
        if self._published:
            self._registry.register_recall(
                factory, extension_id=self.name, scope=self._scope
            )
        else:
            self._draft.recall_factories.append(factory)
            self._maybe_publish()

    def add_provider(self, name: str, factory: ProviderFactory) -> None:
        """注册 provider 工厂：(AgentScope) -> Provider | None。"""
        if not name:
            raise ValueError("provider name 不能为空")
        if self._published:
            self._registry.register_provider(name, factory, scope=self._scope)
        else:
            if name in self._draft.provider_factories:
                raise ValueError(f"Provider '{name}' already registered")
            self._draft.provider_factories[name] = factory
            self._maybe_publish()

    register_provider = add_provider

    def add_disposer(self, disposer: Disposer) -> None:
        """登记本 extension 的生命周期释放动作。"""
        if not callable(disposer):
            raise TypeError("disposer 必须可调用")
        self._scope.add_disposer(disposer)

    def publish(self) -> None:
        self._registry.publish(
            self.name,
            self._draft,
            tool_bus=self._tool_bus,
            scope=self._scope,
            extension_version=self._extension_version,
        )
        self._draft = ExtensionDraft()
        self._published = True

    def _maybe_publish(self) -> None:
        if not self._defer_publish and not self._published:
            self.publish()
