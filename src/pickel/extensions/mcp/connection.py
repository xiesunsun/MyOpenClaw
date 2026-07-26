"""单个 stdio MCP server 的连接生命周期。

stdio_client / ClientSession 的 anyio cancel scope 要求「进入与退出在同一
asyncio 任务」，所以整个 async-with 栈由一个专属背景任务（_run）持有，
open/close 只跟它交换事件。call_tool 可以从任意任务调（SDK 按请求 id 分发）。
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.shared.exceptions import McpError
import mcp.types

from pickel.extensions.mcp.config import McpServerSpec

logger = logging.getLogger(__name__)

_OPEN_TIMEOUT_S = 10.0
_CALL_TIMEOUT_S = 60.0


class McpConnectionError(RuntimeError):
    pass


class McpConnection:
    def __init__(self, spec: McpServerSpec) -> None:
        self.spec = spec
        self.tools: list[mcp.types.Tool] = []
        self._session: ClientSession | None = None
        self._ready = asyncio.Event()
        self._shutdown = asyncio.Event()
        self._runner: asyncio.Task[None] | None = None
        self._error: BaseException | None = None
        self._dead = False

    async def open(self) -> None:
        self._runner = asyncio.create_task(
            self._run(), name=f"mcp-connection-{self.spec.name}"
        )
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=_OPEN_TIMEOUT_S)
        except asyncio.TimeoutError:
            await self.close()
            raise McpConnectionError(
                f"MCP server '{self.spec.name}' did not become ready "
                f"within {_OPEN_TIMEOUT_S:.0f}s"
            ) from None
        if self._error is not None:
            raise McpConnectionError(
                f"MCP server '{self.spec.name}' failed to start: {self._error}"
            ) from self._error

    async def _run(self) -> None:
        params = StdioServerParameters(
            command=self.spec.command,
            args=list(self.spec.args),
            env={**os.environ, **self.spec.env},
        )
        try:
            async with stdio_client(params) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    self.tools = (await session.list_tools()).tools
                    self._session = session
                    self._ready.set()
                    await self._shutdown.wait()
        except BaseException as exc:
            self._error = exc
        finally:
            self._session = None
            self._ready.set()

    def is_alive(self) -> bool:
        # 子进程死掉时 _run 仍阻塞在 _shutdown.wait()，runner.done() 探测不到；
        # 死亡由 call_tool 的异常类型判定并置 _dead。
        return (
            not self._dead
            and self._session is not None
            and self._runner is not None
            and not self._runner.done()
        )

    async def call_tool(
        self, name: str, arguments: dict[str, Any]
    ) -> mcp.types.CallToolResult:
        if not self.is_alive():
            raise McpConnectionError(f"MCP server '{self.spec.name}' is not connected")
        session = self._session
        assert session is not None
        try:
            return await asyncio.wait_for(
                session.call_tool(name, arguments), timeout=_CALL_TIMEOUT_S
            )
        except asyncio.TimeoutError:
            raise
        except (anyio.ClosedResourceError, anyio.BrokenResourceError) as exc:
            self._dead = True
            raise McpConnectionError(
                f"connection to MCP server '{self.spec.name}' lost: {exc}"
            ) from exc
        except McpError as exc:
            # 工具级错误走 CallToolResult.isError，不会抛异常；
            # 抛 McpError 的是协议层故障，其中 Connection closed 即连接死
            if "connection closed" in str(exc).lower():
                self._dead = True
                raise McpConnectionError(
                    f"connection to MCP server '{self.spec.name}' lost: {exc}"
                ) from exc
            raise

    async def close(self) -> None:
        self._shutdown.set()
        if self._runner is None:
            return
        try:
            await asyncio.wait_for(asyncio.shield(self._runner), timeout=5.0)
        except (asyncio.TimeoutError, Exception):
            self._runner.cancel()
            try:
                await self._runner
            except (asyncio.CancelledError, Exception):
                pass
