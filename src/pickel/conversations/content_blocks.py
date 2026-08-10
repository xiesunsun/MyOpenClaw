"""AgentMessage 的版本化内容块持久合同。

ArtifactBlock 是目标态多模态合同；ImageContent 仅供旧 Runtime 切换期间读取。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal

from pickel.artifacts.artifact import (
    ArtifactReference,
    artifact_reference_from_dict,
)


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
class ArtifactBlock:
    """Provider-neutral 多模态块；字节通过 ArtifactReference 解析。"""

    artifact: ArtifactReference
    alt_text: str | None = None
    type: Literal["artifact"] = "artifact"


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


ContentBlock = (
    TextContent | ImageContent | ArtifactBlock | ThinkingContent | ToolCallContent
)
UserContent = TextContent | ImageContent | ArtifactBlock
AssistantContent = TextContent | ThinkingContent | ToolCallContent
ToolResultContent = TextContent | ImageContent | ArtifactBlock


def content_block_to_dict(block: ContentBlock) -> dict[str, Any]:
    """将 content block 序列化为可 JSON 落盘的 dict。"""
    if isinstance(block, ArtifactBlock):
        return {
            "type": block.type,
            "artifact": block.artifact.content_dict(),
            "alt_text": block.alt_text,
        }
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
    if block_type == "artifact":
        return ArtifactBlock(
            artifact=artifact_reference_from_dict(data["artifact"]),
            alt_text=(
                str(data["alt_text"]) if data.get("alt_text") is not None else None
            ),
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
