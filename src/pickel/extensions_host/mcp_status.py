"""MCP extension 向 Runtime 暴露的只读状态合同。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class McpServerStatusSnapshot:
    """单个 MCP server 的最后已知状态；不包含配置密钥或启动参数。"""

    name: str
    status: str
    transport: str = "stdio"
    config_scope: str | None = None
    protocol_version: str | None = None
    implementation_name: str | None = None
    implementation_version: str | None = None
    discovered_tools: int = 0
    last_error: str | None = None


@dataclass(frozen=True)
class McpStatusSnapshot:
    servers: tuple[McpServerStatusSnapshot, ...] = ()
    diagnostics: tuple[str, ...] = ()


class McpStatusSource(Protocol):
    """进程级 MCP 状态源；读取不得触发连接、重连或健康检查。"""

    def snapshot(self) -> McpStatusSnapshot: ...
