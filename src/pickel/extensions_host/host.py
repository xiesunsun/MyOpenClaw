"""给 extension 用的宿主 API。

ExtensionHost 是 per-extension 实例：loader 为每个 extension 单独构造一个，
绑定它的名字与它那段配置。extension 因此不需要（也无法）自报名字。
"""

from __future__ import annotations

from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from pickel.extensions_host.errors import ExtensionConfigError
from pickel.extensions_host.registry import ExtensionRegistry, Factory
from pickel.tools.base import BaseTool
from pickel.tools.bus import ToolBus, ToolSource

T = TypeVar("T", bound=BaseModel)


class ExtensionHost:
    def __init__(
        self,
        *,
        name: str,
        config_section: dict[str, Any] | None,
        tool_bus: ToolBus,
        registry: ExtensionRegistry,
    ) -> None:
        self.name = name
        self._config_section = config_section
        self._tool_bus = tool_bus
        self._registry = registry
        registry.note_extension(name)

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

    def register_tool(self, tool: BaseTool) -> str:
        """注册进程级工具。最终名为 ext__<extension>__<tool>。"""
        return self._tool_bus.register(
            tool,
            source=ToolSource.EXTENSION,
            origin=self.name,
        )

    def add_hook_handler(self, factory: Factory) -> None:
        """注册 hook handler 工厂：(AgentScope) -> handler | None。

        handler 只需实现感兴趣的方法（duck typing，见 hooks/lifecycle.py 的 _call）。
        """
        self._registry.hook_factories.append(factory)

    def add_recall_source(self, factory: Factory) -> None:
        """注册召回源工厂：(AgentScope) -> Recall | None。"""
        self._registry.recall_factories.append(factory)

    def add_session_sync(self, factory: Factory) -> None:
        """注册会话同步工厂：(AgentScope) -> SessionSync | None。"""
        self._registry.sync_factories.append(factory)
