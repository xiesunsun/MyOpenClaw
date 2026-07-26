"""单个 MCP server 的运行时编排：注册、调用、重连一次、卸载。"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import mcp.types

from pickel.extensions.mcp.config import McpServerSpec
from pickel.extensions.mcp.connection import McpConnection, McpConnectionError
from pickel.extensions.mcp.proxy import McpProxyTool

logger = logging.getLogger(__name__)


class McpServerRuntime:
    def __init__(self, *, spec: McpServerSpec, host: Any) -> None:
        self.spec = spec
        self._host = host
        self._connection: McpConnection | None = None
        self._reconnect_lock = asyncio.Lock()

    async def start(self) -> None:
        self._connection = McpConnection(self.spec)
        await self._connection.open()
        self._register_tools()

    async def call(
        self, tool_name: str, arguments: dict[str, Any]
    ) -> mcp.types.CallToolResult:
        connection = self._connection
        if connection is None or not connection.is_alive():
            await self._reconnect()
            connection = self._connection
            assert connection is not None
        try:
            return await connection.call_tool(tool_name, arguments)
        except McpConnectionError:
            await self._reconnect()
            assert self._connection is not None
            # 重试恰好一次；这里再失败就任由异常出去（proxy 转 is_error）
            return await self._connection.call_tool(tool_name, arguments)

    async def close(self) -> None:
        if self._connection is not None:
            await self._connection.close()
            self._connection = None
        self._host.unregister_mcp_origin(self.spec.name)

    def _register_tools(self) -> None:
        # 先卸后注：server 升级后消失的工具被剔除
        self._host.unregister_mcp_origin(self.spec.name)
        assert self._connection is not None
        for tool in self._connection.tools:
            self._host.register_mcp_tool(McpProxyTool(self, tool), server=self.spec.name)

    async def _reconnect(self) -> None:
        async with self._reconnect_lock:
            if self._connection is not None and self._connection.is_alive():
                return  # 并发失败的其他调用已经重连好了
            logger.warning("Reconnecting to MCP server '%s'", self.spec.name)
            if self._connection is not None:
                await self._connection.close()
                self._connection = None
            connection = McpConnection(self.spec)
            try:
                await connection.open()
            except McpConnectionError:
                self._host.unregister_mcp_origin(self.spec.name)
                raise
            self._connection = connection
            self._register_tools()
