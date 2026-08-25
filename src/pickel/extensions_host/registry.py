"""Extension 贡献的收集与求值。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
import logging
import inspect
from typing import Any, Protocol

from pickel.agents.agent_package import ExtensionVersion
from pickel.extensions_host.mcp_status import McpStatusSource
from pickel.extensions_host.event_processor import (
    ConversationExtensionContext,
    ConversationProcessorFactory,
    EventProcessorRegistration,
    ResolvedEventProcessor,
)
from pickel.tools.bus import ToolSource, qualified_name

logger = logging.getLogger(__name__)

# per-agent 扩展点注册的是工厂：返回 None 表示该 agent 不启用这项贡献
Factory = Callable[["AgentScope"], Any]
ProviderFactory = Any
Disposer = Callable[[], Any]


class ContributionLease(Protocol):
    """一个注册或外部资源的精确、幂等释放能力。"""

    async def close(self) -> None: ...


class CallbackLease(ContributionLease):
    """将同步或异步关闭动作统一为 async lease。"""

    def __init__(self, callback: Disposer) -> None:
        if not callable(callback):
            raise TypeError("lease close 必须可调用")
        self._callback = callback
        self._closed = False

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        outcome = self._callback()
        if inspect.isawaitable(outcome):
            await outcome


@dataclass
class ExtensionDraft:
    """Extension setup 期间的业务贡献草稿，不属于生命周期 Scope。"""

    tools: list[Any] = field(default_factory=list)
    mcp_tools: list[tuple[Any, str]] = field(default_factory=list)
    hook_factories: list[Factory] = field(default_factory=list)
    recall_factories: list[Factory] = field(default_factory=list)
    event_processors: list[EventProcessorRegistration] = field(default_factory=list)
    provider_factories: dict[str, ProviderFactory] = field(default_factory=dict)
    mcp_status_source: McpStatusSource | None = None


def _remove_identity(items: list[Any], captured: Any) -> None:
    """按对象身份删除一项，绝不按名字猜测。"""
    for index, item in enumerate(items):
        if item is captured:
            del items[index]
            return


class _MappingLease(ContributionLease):
    def __init__(self, mapping: dict[Any, Any], key: Any, captured: Any) -> None:
        self._mapping = mapping
        self._key = key
        self._captured = captured
        self._closed = False

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._mapping.get(self._key) is self._captured:
            del self._mapping[self._key]


class _ListLease(ContributionLease):
    def __init__(self, values: list[Any], captured: Any) -> None:
        self._values = values
        self._captured = captured
        self._closed = False

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        _remove_identity(self._values, self._captured)


class _CompositeLease(ContributionLease):
    """一个注册同时存在于全局视图和 extension 局部视图时的 lease。"""

    def __init__(self, leases: list[ContributionLease]) -> None:
        self._leases = leases
        self._closed = False

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for lease in reversed(self._leases):
            await lease.close()


@dataclass(frozen=True)
class AgentScope:
    """工厂求值时的 agent 上下文。

    工厂拿不到 Run / Session —— 求值时它们还不存在。
    需要会话级状态的 extension 只能在 hook handler 的事件里拿。
    """

    agent_id: str
    app_config: Any


class ContributionScope:
    """精确 lease 和 child scope 的 LIFO 生命周期边界。"""

    def __init__(self, name: str | None = None) -> None:
        self.name = name
        self._items: list[ContributionLease | ContributionScope] = []
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    def own(self, lease: ContributionLease) -> ContributionLease:
        if self._closed:
            raise RuntimeError("ContributionScope 已关闭")
        if not hasattr(lease, "close"):
            raise TypeError("scope 只能拥有带 close() 的 lease")
        self._items.append(lease)
        return lease

    def child(self, name: str) -> "ContributionScope":
        child = ContributionScope(name)
        self.own(child)
        return child

    def add_disposer(self, disposer: Disposer) -> ContributionLease:
        """兼容旧入口；生产清理仍统一由 close() 驱动。"""
        return self.own(CallbackLease(disposer))

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        items = self._items
        self._items = []
        errors: list[Exception] = []
        for item in reversed(items):
            try:
                await item.close()
            except Exception as exc:
                errors.append(exc)
        if errors:
            logger.error(
                "ContributionScope '%s' close 完成但有 %d 个清理错误",
                self.name or "<anonymous>",
                len(errors),
                exc_info=errors[-1],
            )

    async def dispose(self) -> None:
        """兼容旧入口，委托到唯一的 Scope.close 路径。"""
        await self.close()


@dataclass
class ExtensionRegistry:
    """宿主收集到的全部贡献。Boot 从这里取。

    工具不在此列 —— 它们装载时就直接进了 ToolBus（进程级）。
    """

    hook_factories: list[Factory] = field(default_factory=list)
    recall_factories: list[Factory] = field(default_factory=list)
    event_processors: list[EventProcessorRegistration] = field(default_factory=list)
    provider_factories: dict[str, ProviderFactory] = field(default_factory=dict)
    extension_names: list[str] = field(default_factory=list)
    # ExtensionVersion 是 Package 的冻结实现引用；贡献表按 extension_id 分组，
    # 不能再从全局 factory 列表反推某个扩展的实现归属。
    extension_versions: dict[str, ExtensionVersion] = field(default_factory=dict)
    mcp_status_source: McpStatusSource | None = None
    _hook_factories_by_extension: dict[str, list[Factory]] = field(
        default_factory=dict, repr=False
    )
    _recall_factories_by_extension: dict[str, list[Factory]] = field(
        default_factory=dict, repr=False
    )

    def note_extension(self, name: str) -> None:
        if name not in self.extension_names:
            self.extension_names.append(name)

    def publish(
        self,
        name: str,
        draft: ExtensionDraft,
        *,
        tool_bus: Any,
        scope: ContributionScope,
        extension_version: ExtensionVersion | None = None,
    ) -> None:
        """原子发布一个 extension 的全部 draft 贡献。"""
        if extension_version is not None:
            if extension_version.extension_id != name:
                raise ValueError(
                    "ExtensionVersion.extension_id 必须与发布的 extension_id 一致"
                )
            existing_version = self.extension_versions.get(name)
            if existing_version is not None and existing_version != extension_version:
                raise ValueError(f"Extension '{name}' 已发布了不同的 ExtensionVersion")
        duplicate_providers = set(draft.provider_factories).intersection(
            self.provider_factories
        )
        if duplicate_providers:
            duplicate = sorted(duplicate_providers)[0]
            raise ValueError(f"Provider '{duplicate}' already registered")
        if draft.mcp_status_source is not None and self.mcp_status_source is not None:
            raise ValueError("MCP status source is already registered")

        # 先检查所有工具名字，再触碰 ToolBus，确保发布失败不会覆盖旧贡献。
        existing_names = set(tool_bus.list_names())
        staged_names: set[str] = set()
        for tool in draft.tools:
            qualified = qualified_name(tool.spec.name, ToolSource.EXTENSION, name)
            if qualified in existing_names or qualified in staged_names:
                raise ValueError(f"Tool '{qualified}' already registered")
            staged_names.add(qualified)
        for tool, server in draft.mcp_tools:
            qualified = qualified_name(tool.spec.name, ToolSource.MCP, server)
            if qualified in existing_names or qualified in staged_names:
                raise ValueError(f"Tool '{qualified}' already registered")
            staged_names.add(qualified)

        for tool in draft.tools:
            # 注册成功立即交给 Scope；后续 setup 失败仍由同一 close() 路径回滚。
            scope.own(
                tool_bus.register_lease(
                    tool,
                    source=ToolSource.EXTENSION,
                    origin=name,
                )
            )
        for tool, server in draft.mcp_tools:
            scope.own(
                tool_bus.register_lease(
                    tool,
                    source=ToolSource.MCP,
                    origin=server,
                )
            )

        # ToolBus 注册成功后才改变 registry，避免半发布的贡献集。
        extension_count = len(self.extension_names)
        self.note_extension(name)
        if len(self.extension_names) > extension_count:
            extension_name = self.extension_names[-1]
            scope.own(_ListLease(self.extension_names, extension_name))
        for factory in draft.hook_factories:
            self.hook_factories.append(factory)
            scope.own(_ListLease(self.hook_factories, factory))
            self._hook_factories_by_extension.setdefault(name, []).append(factory)
            scope.own(_ListLease(self._hook_factories_by_extension[name], factory))
        for factory in draft.recall_factories:
            self.recall_factories.append(factory)
            scope.own(_ListLease(self.recall_factories, factory))
            self._recall_factories_by_extension.setdefault(name, []).append(factory)
            scope.own(_ListLease(self._recall_factories_by_extension[name], factory))
        for registration in draft.event_processors:
            self.event_processors.append(registration)
            scope.own(_ListLease(self.event_processors, registration))
        if draft.mcp_status_source is not None:
            self.mcp_status_source = draft.mcp_status_source
            scope.own(
                CallbackLease(
                    lambda source=draft.mcp_status_source: self._clear_status(source)
                )
            )
        for provider_name, factory in draft.provider_factories.items():
            self.provider_factories[provider_name] = factory
            scope.own(_MappingLease(self.provider_factories, provider_name, factory))
        if extension_version is not None and name not in self.extension_versions:
            self.extension_versions[name] = extension_version
            scope.own(_MappingLease(self.extension_versions, name, extension_version))

    def register_hook(
        self,
        factory: Factory,
        *,
        extension_id: str | None = None,
        scope: ContributionScope,
    ) -> ContributionLease:
        self.hook_factories.append(factory)
        leases: list[ContributionLease] = [_ListLease(self.hook_factories, factory)]
        if extension_id is not None:
            bucket = self._hook_factories_by_extension.setdefault(extension_id, [])
            bucket.append(factory)
            leases.append(_ListLease(bucket, factory))
        return scope.own(_CompositeLease(leases))

    def register_recall(
        self,
        factory: Factory,
        *,
        extension_id: str | None = None,
        scope: ContributionScope,
    ) -> ContributionLease:
        self.recall_factories.append(factory)
        leases: list[ContributionLease] = [_ListLease(self.recall_factories, factory)]
        if extension_id is not None:
            bucket = self._recall_factories_by_extension.setdefault(extension_id, [])
            bucket.append(factory)
            leases.append(_ListLease(bucket, factory))
        return scope.own(_CompositeLease(leases))

    def register_event_processor(
        self,
        registration: EventProcessorRegistration,
        *,
        scope: ContributionScope,
    ) -> ContributionLease:
        self.event_processors.append(registration)
        return scope.own(_ListLease(self.event_processors, registration))

    def register_provider(
        self,
        name: str,
        factory: ProviderFactory,
        *,
        scope: ContributionScope,
    ) -> ContributionLease:
        if name in self.provider_factories:
            raise ValueError(f"Provider '{name}' already registered")
        self.provider_factories[name] = factory
        return scope.own(_MappingLease(self.provider_factories, name, factory))

    def register_status_source(
        self, source: McpStatusSource, *, scope: ContributionScope
    ) -> ContributionLease:
        if self.mcp_status_source is not None:
            raise ValueError("MCP status source is already registered")
        self.mcp_status_source = source
        return scope.own(CallbackLease(lambda: self._clear_status(source)))

    def resolve_extension_contributions(
        self, expected: ExtensionVersion, scope: AgentScope
    ) -> tuple[tuple[Any, ...], tuple[Any, ...]]:
        """精确匹配当前 Generation 内的扩展，并严格求值其贡献。"""
        actual = self.extension_versions.get(expected.extension_id)
        if actual is None:
            raise LookupError(
                f"Extension '{expected.extension_id}' 未在当前 Generation 注册"
            )
        if actual != expected:
            raise ValueError(
                f"Extension '{expected.extension_id}' 的 ExtensionVersion 不匹配"
            )
        return (
            self._evaluate_exact(
                self._hook_factories_by_extension.get(expected.extension_id, ()),
                scope,
            ),
            self._evaluate_exact(
                self._recall_factories_by_extension.get(expected.extension_id, ()),
                scope,
            ),
        )

    @staticmethod
    def _evaluate_exact(
        factories: tuple[Factory, ...] | list[Factory], scope: AgentScope
    ) -> tuple[Any, ...]:
        results: list[Any] = []
        for factory in factories:
            value = factory(scope)
            if value is not None:
                results.append(value)
        return tuple(results)

    def _clear_status(self, captured: McpStatusSource) -> None:
        if self.mcp_status_source is captured:
            self.mcp_status_source = None

    def providers(self, scope: AgentScope) -> dict[str, Any]:
        """求值 provider factory，单个失败不影响其余 provider。"""
        result: dict[str, Any] = {}
        for name, factory in self.provider_factories.items():
            try:
                provider = factory(scope) if callable(factory) else factory
            except Exception:
                logger.exception("Extension provider factory failed: %s", name)
                continue
            if provider is not None:
                result[name] = provider
        return result

    def hook_handlers(self, scope: AgentScope) -> list[Any]:
        return self._evaluate(self.hook_factories, scope, "hook handler")

    def recall_sources(self, scope: AgentScope) -> list[Any]:
        return self._evaluate(self.recall_factories, scope, "recall source")

    def add_event_processor(
        self,
        *,
        extension_name: str,
        event_types: tuple[type[Any], ...],
        factory: ConversationProcessorFactory,
    ) -> None:
        self.event_processors.append(
            EventProcessorRegistration(
                extension_name=extension_name,
                event_types=event_types,
                factory=factory,
            )
        )

    def resolve_event_processors(
        self,
        context: ConversationExtensionContext,
    ) -> list[ResolvedEventProcessor]:
        resolved: list[ResolvedEventProcessor] = []
        for registration in self.event_processors:
            try:
                processor = registration.factory(context)
            except Exception:
                logger.exception(
                    "Extension event processor factory failed: %s",
                    registration.extension_name,
                )
                continue
            if processor is not None:
                resolved.append(
                    ResolvedEventProcessor(
                        processor=processor,
                        event_types=registration.event_types,
                    )
                )
        return resolved

    @staticmethod
    def _evaluate(factories: list[Factory], scope: AgentScope, label: str) -> list[Any]:
        """逐个求值，过滤 None，单个失败只记日志。"""
        results: list[Any] = []
        for factory in factories:
            try:
                value = factory(scope)
            except Exception:
                logger.exception("Extension %s factory failed", label)
                continue
            if value is not None:
                results.append(value)
        return results
