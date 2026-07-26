"""MCP 工具 → BaseTool 代理。schema 直传，结果拍平为文本。"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import mcp.types

from pickel.tools.base import (
    BaseTool,
    ToolExecutionContext,
    ToolExecutionResult,
    ToolSpec,
)

if TYPE_CHECKING:
    from pickel.extensions.mcp.runtime import McpServerRuntime


class McpProxyTool(BaseTool):
    def __init__(self, runtime: McpServerRuntime, tool: mcp.types.Tool) -> None:
        self.spec = ToolSpec(
            name=tool.name,
            description=tool.description or "",
            input_schema=tool.inputSchema,
        )
        self._runtime = runtime

    async def execute(
        self,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
    ) -> ToolExecutionResult:
        metadata: dict[str, Any] = {
            "server": self._runtime.spec.name,
            "mcp_tool": self.spec.name,
        }
        try:
            result = await self._runtime.call(self.spec.name, arguments)
        except asyncio.TimeoutError:
            return ToolExecutionResult(
                content=f"MCP tool call timed out ({self.spec.name})",
                is_error=True,
                metadata=metadata,
            )
        except Exception as exc:
            return ToolExecutionResult(
                content=f"MCP server '{self._runtime.spec.name}' is unavailable: {exc}",
                is_error=True,
                metadata=metadata,
            )
        parts: list[str] = []
        unsupported: list[str] = []
        for block in result.content:
            if isinstance(block, mcp.types.TextContent):
                parts.append(block.text)
            else:
                parts.append(f"[unsupported content: {block.type}]")
                unsupported.append(block.type)
        if unsupported:
            metadata["unsupported_content"] = unsupported
        return ToolExecutionResult(
            content="\n".join(parts),
            is_error=bool(result.isError),
            metadata=metadata,
        )
