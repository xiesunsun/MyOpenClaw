"""单个 MCP server 的运行时编排：注册、调用、重连一次、卸载。"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import mcp.types

from pickel.extensions.mcp.config import McpServerSpec
from pickel.extensions.mcp.connection import McpConnection, McpConnectionError
from pickel.extensions.mcp.elicitation_mapper import resolve_elicitation
from pickel.extensions.mcp.proxy import McpProxyTool
from pickel.extensions_host.mcp_status import McpServerStatusSnapshot
from pickel.runs.host_calls import HostCallClient, HostCallContext

logger = logging.getLogger(__name__)

_MAX_INPUT_REQUIRED_ROUNDS = 10


class McpServerRuntime:
    def __init__(self, *, spec: McpServerSpec, host: Any) -> None:
        self.spec = spec
        self._host = host
        self._connection: McpConnection | None = None
        self._reconnect_lock = asyncio.Lock()
        self._status = "connecting"
        self._last_error: str | None = None

    async def start(self) -> None:
        self._status = "connecting"
        self._last_error = None
        self._connection = McpConnection(self.spec)
        try:
            await self._connection.open()
            self._register_tools()
        except Exception as exc:
            self._status = "failed"
            self._last_error = _safe_error(exc)
            raise
        self._status = "connected"

    async def call(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        host_calls: HostCallClient | None = None,
        call_context: HostCallContext | None = None,
    ) -> mcp.types.CallToolResult:
        connection = self._connection
        if connection is None or not connection.is_alive():
            await self._reconnect()
            connection = self._connection
            assert connection is not None
        try:
            if host_calls is None or call_context is None:
                return await connection.call_tool(tool_name, arguments)
            return await self._drive_input_required(
                connection=connection,
                tool_name=tool_name,
                arguments=arguments,
                host_calls=host_calls,
                call_context=call_context,
            )
        except McpConnectionError as exc:
            # 请求已经发出时，无法判断 server 是否完成了副作用。
            # 只为后续调用恢复连接，绝不自动重放当前工具。
            try:
                await self._reconnect()
            except McpConnectionError:
                # _reconnect 已卸载不可用工具；当前调用仍应报告原始的未知结果。
                pass
            raise McpConnectionError(
                f"{exc}; tool execution status is unknown and the call was not retried"
            ) from exc

    async def _drive_input_required(
        self,
        *,
        connection: McpConnection,
        tool_name: str,
        arguments: dict[str, Any],
        host_calls: HostCallClient,
        call_context: HostCallContext,
    ) -> mcp.types.CallToolResult:
        responses: mcp.types.InputResponses | None = None
        request_state: str | None = None
        state_only_delay = 0.05
        for round_index in range(_MAX_INPUT_REQUIRED_ROUNDS + 1):
            result = await connection.call_tool_round(
                tool_name,
                arguments,
                input_responses=responses,
                request_state=request_state,
            )
            if isinstance(result, mcp.types.CallToolResult):
                return result
            if round_index >= _MAX_INPUT_REQUIRED_ROUNDS:
                raise McpConnectionError(
                    f"MCP tool '{tool_name}' exceeded {_MAX_INPUT_REQUIRED_ROUNDS} input-required rounds"
                )
            request_state = result.request_state
            if not result.input_requests:
                await asyncio.sleep(state_only_delay)
                state_only_delay = min(state_only_delay * 2, 0.25)
                responses = None
                continue
            state_only_delay = 0.05
            responses = await self._resolve_input_requests(
                input_requests=result.input_requests,
                host_calls=host_calls,
                call_context=call_context,
                tool_name=tool_name,
            )
        raise AssertionError("unreachable")

    async def _resolve_input_requests(
        self,
        *,
        input_requests: mcp.types.InputRequests,
        host_calls: HostCallClient,
        call_context: HostCallContext,
        tool_name: str,
    ) -> mcp.types.InputResponses:
        async def resolve(key: str, request: mcp.types.InputRequest):
            if not isinstance(request, mcp.types.ElicitRequest):
                raise McpConnectionError(
                    f"MCP input request '{request.method}' is not supported"
                )
            context = HostCallContext(
                call_id=f"{call_context.call_id}:{key}",
                session_id=call_context.session_id,
                turn_id=call_context.turn_id,
                step_index=call_context.step_index,
                tool_call_id=call_context.tool_call_id,
                timeout_seconds=call_context.timeout_seconds,
            )
            response = await resolve_elicitation(
                request.params,
                host_calls=host_calls,
                context=context,
                server_name=self.spec.name,
                tool_name=tool_name,
            )
            return key, response

        pairs = await asyncio.gather(
            *(resolve(key, request) for key, request in input_requests.items())
        )
        return dict(pairs)

    async def close(self) -> None:
        try:
            if self._connection is not None:
                await self._connection.close()
        finally:
            self._connection = None
            self._host.unregister_mcp_origin(self.spec.name)
            self._status = "closed"

    def _register_tools(self) -> None:
        # 先卸后注：server 升级后消失的工具被剔除
        self._host.unregister_mcp_origin(self.spec.name)
        assert self._connection is not None
        for tool in self._connection.tools:
            self._host.register_mcp_tool(
                McpProxyTool(self, tool), server=self.spec.name
            )

    async def _reconnect(self) -> None:
        async with self._reconnect_lock:
            if self._connection is not None and self._connection.is_alive():
                return  # 并发失败的其他调用已经重连好了
            self._status = "reconnecting"
            logger.warning("Reconnecting to MCP server '%s'", self.spec.name)
            if self._connection is not None:
                await self._connection.close()
                self._connection = None
            connection = McpConnection(self.spec)
            try:
                await connection.open()
            except McpConnectionError as exc:
                self._host.unregister_mcp_origin(self.spec.name)
                self._status = "failed"
                self._last_error = _safe_error(exc)
                raise
            self._connection = connection
            self._register_tools()
            self._status = "connected"
            self._last_error = None

    def snapshot(self) -> McpServerStatusSnapshot:
        connection = self._connection
        implementation = connection.server_info if connection is not None else None
        return McpServerStatusSnapshot(
            name=self.spec.name,
            status=self._status,
            config_scope=self.spec.config_scope,
            protocol_version=(
                connection.protocol_version if connection is not None else None
            ),
            implementation_name=getattr(implementation, "name", None),
            implementation_version=getattr(implementation, "version", None),
            discovered_tools=(len(connection.tools) if connection is not None else 0),
            last_error=self._last_error,
        )


def _safe_error(exc: Exception) -> str:
    """状态输出只保留有限错误文本，不包含配置 env 或启动参数。"""
    message = str(exc).strip() or type(exc).__name__
    return message[:500]
