"""Extension 贡献的收集与求值。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
import logging
from typing import Any

from pickel.extensions_host.mcp_status import McpStatusSource

logger = logging.getLogger(__name__)

# per-agent 扩展点注册的是工厂：返回 None 表示该 agent 不启用这项贡献
Factory = Callable[["AgentScope"], Any]


@dataclass(frozen=True)
class AgentScope:
    """工厂求值时的 agent 上下文。

    工厂拿不到 Run / Session —— 求值时它们还不存在。
    需要会话级状态的 extension 只能在 hook handler 的事件里拿。
    """

    agent_id: str
    app_config: Any


@dataclass
class ExtensionRegistry:
    """宿主收集到的全部贡献。Boot 从这里取。

    工具不在此列 —— 它们装载时就直接进了 ToolBus（进程级）。
    """

    hook_factories: list[Factory] = field(default_factory=list)
    recall_factories: list[Factory] = field(default_factory=list)
    sync_factories: list[Factory] = field(default_factory=list)
    extension_names: list[str] = field(default_factory=list)
    mcp_status_source: McpStatusSource | None = None

    def note_extension(self, name: str) -> None:
        if name not in self.extension_names:
            self.extension_names.append(name)

    def hook_handlers(self, scope: AgentScope) -> list[Any]:
        return self._evaluate(self.hook_factories, scope, "hook handler")

    def recall_sources(self, scope: AgentScope) -> list[Any]:
        return self._evaluate(self.recall_factories, scope, "recall source")

    def session_syncs(self, scope: AgentScope) -> list[Any]:
        return self._evaluate(self.sync_factories, scope, "session sync")

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
