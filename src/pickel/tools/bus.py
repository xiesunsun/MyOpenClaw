"""工具总线：进程级注册表 + turn 级激活集与快照。"""

from __future__ import annotations

from collections.abc import Iterable
import fnmatch
from dataclasses import dataclass, replace
from enum import StrEnum

from pickel.tools.base import BaseTool


class ToolSource(StrEnum):
    """工具来源。三者命名空间前缀各自独立：builtin 裸名、mcp__、ext__。

    MCP 工具跑在子进程，extension 工具跑在本进程内 —— 执行位置与信任级别不同。
    """

    BUILTIN = "builtin"
    MCP = "mcp"
    EXTENSION = "extension"


class ToolNameConflictError(Exception):
    """不同来源注册了同名工具。命名空间前缀本应避免，撞了就是 bug，不静默覆盖。"""


@dataclass(frozen=True)
class ToolEntry:
    """总线中的一条工具记录。name 是含命名空间前缀的最终名，全总线唯一。"""

    name: str
    tool: BaseTool
    source: ToolSource
    version: str | None = None
    origin: str | None = None
    enabled: bool = True


@dataclass(frozen=True)
class ToolActivation:
    """一次 turn 的激活集计算输入。

    allowed 是 agent.yaml 的 tools 白名单，人的授权、硬边界；
    agent_disabled 是 agent 通过 tool_set_active 自我收窄的部分。
    求交顺序保证 agent 只能收窄、永不扩张。
    """

    allowed: frozenset[str]
    agent_disabled: frozenset[str] = frozenset()

    def with_agent_disabled(self, names: Iterable[str]) -> ToolActivation:
        return replace(self, agent_disabled=self.agent_disabled | frozenset(names))

    def with_agent_enabled(self, names: Iterable[str]) -> ToolActivation:
        return replace(self, agent_disabled=self.agent_disabled - frozenset(names))


@dataclass(frozen=True)
class ToolSnapshot:
    """AgentRun 内不可变的工具视图。上下文构建与工具执行的唯一来源。

    不提供 definitions()：ToolDefinition 属 context 层，转换留在 build_tool_definitions，
    避免 tools 层反向依赖 context 层。
    """

    entries: tuple[ToolEntry, ...]

    def get(self, name: str) -> ToolEntry | None:
        for entry in self.entries:
            if entry.name == name:
                return entry
        return None

    def find(self, name: str) -> BaseTool | None:
        entry = self.get(name)
        return entry.tool if entry is not None else None

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(entry.name for entry in self.entries)


_PREFIX_BY_SOURCE = {
    ToolSource.MCP: "mcp",
    ToolSource.EXTENSION: "ext",
}


def qualified_name(spec_name: str, source: ToolSource, origin: str | None) -> str:
    """按来源计算最终名。

    内置工具用裸名；MCP 工具加 mcp__<server>__，extension 工具加 ext__<extension>__。
    两者前缀不同：MCP 工具跑在子进程里，extension 工具跑在本进程内，
    执行位置与信任级别都不同，名字上就要能区分。
    """
    if source is ToolSource.BUILTIN:
        return spec_name
    if not origin:
        raise ValueError(f"source '{source}' requires a non-empty origin")
    if "__" in origin:
        # 否则 origin 'a' + 工具 'b__c' 与 origin 'a__b' + 工具 'c' 会撞成同一个名字
        raise ValueError(f"origin '{origin}' must not contain '__'")
    return f"{_PREFIX_BY_SOURCE[source]}__{origin}__{spec_name}"


def _activation_allows(name: str, allowed: frozenset[str]) -> bool:
    if name in allowed:
        return True
    return any(
        "*" in pattern and fnmatch.fnmatchcase(name, pattern) for pattern in allowed
    )


class ToolBus:
    """进程级工具注册表。可变，跨 Run / session / reload 存活。"""

    def __init__(self) -> None:
        self._entries: dict[str, ToolEntry] = {}

    def register(
        self,
        tool: BaseTool,
        *,
        source: ToolSource,
        version: str | None = None,
        origin: str | None = None,
    ) -> str:
        """注册工具，返回最终名。同来源同 origin 视为重新注册（保留 enabled）。"""
        name = qualified_name(tool.spec.name, source, origin)
        existing = self._entries.get(name)
        if existing is not None and (
            existing.source is not source or existing.origin != origin
        ):
            raise ToolNameConflictError(
                f"Tool '{name}' already registered by source '{existing.source}'"
            )
        self._entries[name] = ToolEntry(
            name=name,
            tool=tool,
            source=source,
            version=version,
            origin=origin,
            enabled=existing.enabled if existing is not None else True,
        )
        return name

    def unregister(self, name: str) -> None:
        self._entries.pop(name, None)

    def unregister_origin(self, source: ToolSource, origin: str) -> list[str]:
        """卸掉某来源某 origin 的全部工具，返回被卸的名字。

        E1 用于 extension 重载/卸载，T2 用于 MCP server 断开。
        """
        names = [
            name
            for name, entry in self._entries.items()
            if entry.source is source and entry.origin == origin
        ]
        for name in names:
            del self._entries[name]
        return names

    def set_enabled(self, name: str, enabled: bool) -> None:
        entry = self.get(name)
        self._entries[name] = replace(entry, enabled=enabled)

    def get(self, name: str) -> ToolEntry:
        try:
            return self._entries[name]
        except KeyError as exc:
            raise KeyError(f"Unknown tool: {name}") from exc

    def list(self, *, source: ToolSource | None = None) -> list[ToolEntry]:
        entries = list(self._entries.values())
        if source is None:
            return entries
        return [entry for entry in entries if entry.source is source]

    def list_names(self, *, source: ToolSource | None = None) -> list[str]:
        return [entry.name for entry in self.list(source=source)]

    def snapshot(self, activation: ToolActivation) -> ToolSnapshot:
        """按激活集三层求交，取本 turn 的不可变视图。"""
        entries = tuple(
            entry
            for entry in self._entries.values()
            if entry.enabled
            and _activation_allows(entry.name, activation.allowed)
            and entry.name not in activation.agent_disabled
        )
        return ToolSnapshot(entries=entries)

    def missing_names(self, activation: ToolActivation) -> list[str]:
        """白名单里存在、bus 中却没有的名字。调用方据此记 warning，不视为错误。

        通配模式（含 *）只有在 bus 里没有任何名字匹配它时才算 missing。
        """
        missing = []
        for name in sorted(activation.allowed):
            if "*" in name:
                if not any(
                    fnmatch.fnmatchcase(existing, name) for existing in self._entries
                ):
                    missing.append(name)
            elif name not in self._entries:
                missing.append(name)
        return missing


def bus_with(tools: Iterable[BaseTool]) -> ToolBus:
    """用一组工具建一个私有总线，全部登记为内置来源。

    供 Run.open 的 tools= 便捷路径与直接构造 Run 的测试共用。
    """
    bus = ToolBus()
    for candidate in tools:
        bus.register(candidate, source=ToolSource.BUILTIN)
    return bus
