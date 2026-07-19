"""Content blocks for AgentMessage（持久消息合同）。

第一版块类型：Text / Image / Thinking / ToolCall。
ThinkingContent 无 opaque 字段。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal


@dataclass(frozen=True)
class TextContent:
    text: str
    type: Literal["text"] = "text"


@dataclass(frozen=True)
class ImageContent:
    media_type: str
    data_base64: str | None = None
    url: str | None = None
    type: Literal["image"] = "image"


@dataclass(frozen=True)
class ThinkingContent:
    text: str
    signature: str | None = None
    type: Literal["thinking"] = "thinking"


@dataclass(frozen=True)
class ToolCallContent:
    id: str
    name: str
    arguments: dict[str, Any]
    thought_signature: str | None = None
    type: Literal["tool_call"] = "tool_call"


ContentBlock = TextContent | ImageContent | ThinkingContent | ToolCallContent
UserContent = TextContent | ImageContent
AssistantContent = TextContent | ThinkingContent | ToolCallContent
ToolResultContent = TextContent | ImageContent


def content_block_to_dict(block: ContentBlock) -> dict[str, Any]:
    """将 content block 序列化为可 JSON 落盘的 dict。"""
    return asdict(block)


def content_block_from_dict(data: dict[str, Any]) -> ContentBlock:
    """从 dict 还原 content block。"""
    if not isinstance(data, dict):
        raise TypeError("content block 必须是 dict")
    block_type = data.get("type")
    if block_type == "text":
        return TextContent(text=data["text"])
    if block_type == "image":
        return ImageContent(
            media_type=data["media_type"],
            data_base64=data.get("data_base64"),
            url=data.get("url"),
        )
    if block_type == "thinking":
        return ThinkingContent(
            text=data["text"],
            signature=data.get("signature"),
        )
    if block_type == "tool_call":
        arguments = data.get("arguments")
        if not isinstance(arguments, dict):
            raise TypeError("ToolCallContent.arguments 必须是 dict")
        return ToolCallContent(
            id=data["id"],
            name=data["name"],
            arguments=dict(arguments),
            thought_signature=data.get("thought_signature"),
        )
    raise ValueError(f"未知 content block type: {block_type!r}")


def content_blocks_to_list(blocks: list[ContentBlock]) -> list[dict[str, Any]]:
    return [content_block_to_dict(block) for block in blocks]


def content_blocks_from_list(items: list[dict[str, Any]]) -> list[ContentBlock]:
    if not isinstance(items, list):
        raise TypeError("content 必须是 list")
    return [content_block_from_dict(item) for item in items]
