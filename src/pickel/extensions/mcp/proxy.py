"""MCP 工具 → BaseTool 代理。schema 直传，结果拍平为文本。"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

import mcp.types

from pickel.tools.base import (
    BaseTool,
    ToolExecutionContext,
    ToolExecutionResult,
    ToolSpec,
)
from pickel.conversations.content_blocks import ImageContent, TextContent
from pickel.runs.host_calls import HostCallContext

if TYPE_CHECKING:
    from pickel.extensions.mcp.runtime import McpServerRuntime


class McpProxyTool(BaseTool):
    def __init__(self, runtime: McpServerRuntime, tool: mcp.types.Tool) -> None:
        self.spec = ToolSpec(
            name=tool.name,
            description=tool.description or "",
            input_schema=tool.input_schema,
            output_schema=tool.output_schema,
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
            host_calls = context.services.host_calls
            if host_calls is None:
                result = await self._runtime.call(self.spec.name, arguments)
            else:
                result = await self._runtime.call(
                    self.spec.name,
                    arguments,
                    host_calls=host_calls,
                    call_context=HostCallContext(
                        session_id=context.session_id,
                        turn_id=context.turn_id,
                        step_index=context.step_index,
                        tool_call_id=context.tool_call_id,
                    ),
                )
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
        text_parts: list[str] = []
        content_blocks: list[TextContent | ImageContent] = []
        unsupported: list[str] = []
        for block in result.content:
            if isinstance(block, mcp.types.TextContent):
                text_parts.append(block.text)
                content_blocks.append(TextContent(text=block.text))
            elif isinstance(block, mcp.types.ImageContent):
                content_blocks.append(
                    ImageContent(
                        media_type=block.mime_type,
                        data_base64=block.data,
                    )
                )
            else:
                unsupported.append(block.type)
        structured_content = result.structured_content
        if not text_parts and structured_content is not None:
            text_parts.append(
                json.dumps(structured_content, ensure_ascii=False, default=str)
            )
        if not text_parts and unsupported:
            text_parts.extend(f"[unsupported content: {kind}]" for kind in unsupported)
        if unsupported:
            metadata["unsupported_content"] = unsupported
            metadata["unsupported_mcp_content"] = [
                block.model_dump(mode="json", by_alias=True, exclude_none=True)
                for block in result.content
                if not isinstance(
                    block, (mcp.types.TextContent, mcp.types.ImageContent)
                )
            ]
        return ToolExecutionResult(
            content="\n".join(text_parts),
            content_blocks=content_blocks,
            structured_content=structured_content,
            is_error=bool(result.is_error),
            metadata=metadata,
        )
