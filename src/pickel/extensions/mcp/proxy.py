"""MCP 工具 → BaseTool 代理。"""

from __future__ import annotations

import asyncio
import base64
from typing import TYPE_CHECKING, Any

import mcp.types

from pickel.tools.base import (
    BaseTool,
    ToolExecutionContext,
    ToolExecutionError,
    ToolSpec,
)
from pickel.conversations.content_blocks import (
    ArtifactBlock,
    TextBlock,
    ToolResultContent,
    content_block_from_dict,
    content_block_to_dict,
)
from pickel.runtime.host_calls import HostCallContext

if TYPE_CHECKING:
    from pickel.extensions.mcp.runtime import McpServerRuntime


class McpProxyTool(BaseTool):
    def __init__(self, runtime: McpServerRuntime, tool: mcp.types.Tool) -> None:
        # MCP 的 outputSchema 可缺省；代理仍必须冻结一个明确的 envelope。
        # server schema 只约束 envelope 的 structured 字段。
        structured_schema = tool.output_schema
        if not isinstance(structured_schema, dict):
            structured_schema = {}
        output_schema = {
            "type": "object",
            "properties": {
                "content_blocks": {
                    "type": "array",
                    "items": {"type": "object"},
                },
                "structured": {
                    "anyOf": [structured_schema, {"type": "null"}],
                },
                "unsupported_content": {
                    "type": "array",
                    "items": {"type": "string"},
                },
            },
            "required": [
                "content_blocks",
                "structured",
                "unsupported_content",
            ],
            "additionalProperties": False,
        }
        self.spec = ToolSpec(
            name=tool.name,
            description=tool.description or "",
            input_schema=tool.input_schema,
            output_schema=output_schema,
        )
        self._runtime = runtime

    async def execute(
        self,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
    ) -> Any:
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
                        identity=context.identity,
                    ),
                )
        except asyncio.TimeoutError:
            raise ToolExecutionError(f"MCP tool call timed out ({self.spec.name})")
        except Exception as exc:
            raise ToolExecutionError(
                f"MCP server '{self._runtime.spec.name}' is unavailable: {exc}"
            ) from exc
        content_blocks: list[TextBlock | ArtifactBlock] = []
        unsupported: list[str] = []
        for block in result.content:
            if isinstance(block, mcp.types.TextContent):
                content_blocks.append(TextBlock(text=block.text))
            elif isinstance(block, mcp.types.ImageContent):
                artifact_service = context.services.artifact_service
                if artifact_service is None:
                    unsupported.append(block.type)
                    continue
                reference = artifact_service.create_artifact(
                    data=base64.b64decode(block.data, validate=True),
                    media_type=block.mime_type,
                )
                content_blocks.append(ArtifactBlock(artifact=reference))
            else:
                unsupported.append(block.type)
        if result.is_error:
            raise ToolExecutionError("MCP 工具返回错误")
        return {
            "content_blocks": [
                content_block_to_dict(block) for block in content_blocks
            ],
            "structured": result.structured_content,
            "unsupported_content": unsupported,
        }

    def render(self, validated_value: Any) -> tuple[ToolResultContent, ...]:
        """纯还原 MCP 返回的 provider-neutral content blocks。"""
        if not isinstance(validated_value, dict):
            return super().render(validated_value)
        structured = validated_value.get("structured")
        blocks = validated_value.get("content_blocks")
        if not isinstance(blocks, list):
            if structured is not None:
                return super().render(structured)
            return super().render(validated_value)
        rendered: list[ToolResultContent] = []
        for block in blocks:
            try:
                parsed = content_block_from_dict(block)
            except (TypeError, ValueError, KeyError):
                continue
            if isinstance(parsed, (TextBlock, ArtifactBlock)):
                rendered.append(parsed)
        if not rendered and structured is not None:
            return super().render(structured)
        for kind in validated_value.get("unsupported_content", ()):
            rendered.append(TextBlock(text=f"[unsupported content: {kind}]"))
        if not rendered:
            return super().render(None)
        return tuple(rendered)
