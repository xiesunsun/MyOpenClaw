"""单个 stdio MCP server 的连接生命周期。

Client / stdio transport 的 async-with 生命周期由一个专属背景任务（_run）
持有，open/close 只跟它交换事件。call_tool 可以从任意任务调，SDK 负责请求分发。
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

import anyio
from mcp import Client, MCPError, StdioServerParameters
from mcp.client.stdio import stdio_client
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
        self.protocol_version: str | None = None
        self.server_info: mcp.types.Implementation | None = None
        self.server_capabilities: mcp.types.ServerCapabilities | None = None
        self._client: Client | None = None
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
            # 保持既有行为：stdio server 继承宿主环境，配置中的 env 覆盖同名项。
            env={**os.environ, **self.spec.env},
        )
        try:
            async with Client(
                stdio_client(params),
                mode="auto",
                cache=None,
                # 声明 Runtime 具备 elicitation broker；具体 provider 在每次
                # tool call 的 HostCallClient 上解析。旧式独立 callback 安全拒绝。
                elicitation_callback=_reject_standalone_elicitation,
            ) as client:
                self.tools = await _list_all_tools(client)
                self.protocol_version = client.protocol_version
                self.server_info = client.server_info
                self.server_capabilities = client.server_capabilities
                self._client = client
                self._ready.set()
                await self._shutdown.wait()
        except BaseException as exc:
            self._error = exc
        finally:
            self._client = None
            self._ready.set()

    def is_alive(self) -> bool:
        # 子进程死掉时 _run 仍阻塞在 _shutdown.wait()，runner.done() 探测不到；
        # 死亡由 call_tool 的异常类型判定并置 _dead。
        return (
            not self._dead
            and self._client is not None
            and self._runner is not None
            and not self._runner.done()
        )

    async def call_tool(
        self, name: str, arguments: dict[str, Any]
    ) -> mcp.types.CallToolResult:
        if not self.is_alive():
            raise McpConnectionError(f"MCP server '{self.spec.name}' is not connected")
        client = self._client
        assert client is not None
        try:
            return await asyncio.wait_for(
                client.call_tool(name, arguments), timeout=_CALL_TIMEOUT_S
            )
        except asyncio.TimeoutError:
            raise
        except (anyio.ClosedResourceError, anyio.BrokenResourceError) as exc:
            self._dead = True
            raise McpConnectionError(
                f"connection to MCP server '{self.spec.name}' lost: {exc}"
            ) from exc
        except MCPError as exc:
            # 工具级错误走 CallToolResult.is_error，不会抛异常；
            # CONNECTION_CLOSED 才代表传输连接已不可继续使用。
            if exc.code == mcp.types.CONNECTION_CLOSED:
                self._dead = True
                raise McpConnectionError(
                    f"connection to MCP server '{self.spec.name}' lost: {exc}"
                ) from exc
            raise

    async def call_tool_round(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        input_responses: mcp.types.InputResponses | None = None,
        request_state: str | None = None,
    ) -> mcp.types.CallToolResult | mcp.types.InputRequiredResult:
        """执行一轮 tools/call，把 MRTR 驱动权交给 Runtime。"""
        if not self.is_alive():
            raise McpConnectionError(f"MCP server '{self.spec.name}' is not connected")
        client = self._client
        assert client is not None
        try:
            result = await asyncio.wait_for(
                client.session.call_tool(
                    name,
                    arguments,
                    input_responses=input_responses,
                    request_state=request_state,
                    allow_input_required=True,
                ),
                timeout=_CALL_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            raise
        except (anyio.ClosedResourceError, anyio.BrokenResourceError) as exc:
            self._dead = True
            raise McpConnectionError(
                f"connection to MCP server '{self.spec.name}' lost: {exc}"
            ) from exc
        except MCPError as exc:
            if exc.code == mcp.types.CONNECTION_CLOSED:
                self._dead = True
                raise McpConnectionError(
                    f"connection to MCP server '{self.spec.name}' lost: {exc}"
                ) from exc
            raise
        if isinstance(
            result, (mcp.types.CallToolResult, mcp.types.InputRequiredResult)
        ):
            return result
        raise McpConnectionError(
            f"MCP server '{self.spec.name}' returned an invalid tools/call result"
        )

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


async def _list_all_tools(client: Client) -> list[mcp.types.Tool]:
    """取完全部分页；MCP server 不保证 tools/list 只有一页。"""
    tools: list[mcp.types.Tool] = []
    cursor: str | None = None
    while True:
        page = await client.list_tools(cursor=cursor)
        tools.extend(page.tools)
        if page.next_cursor is None:
            return tools
        cursor = page.next_cursor


async def _reject_standalone_elicitation(_context, _params):
    """现代 MRTR 由 call_tool_round 处理；缺少调用身份的旧式回调安全拒绝。"""
    return mcp.types.ErrorData(
        code=mcp.types.INVALID_REQUEST,
        message="Standalone elicitation is not supported by this runtime",
    )
