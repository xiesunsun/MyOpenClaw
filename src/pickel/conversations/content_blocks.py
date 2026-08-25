"""AgentMessage 的版本化多模态 Block 持久化合同。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Mapping, Sequence

from pickel.artifacts.artifact import (
    ArtifactReference,
    artifact_reference_from_dict,
)
from pickel.shared.frozen_json import freeze_json_object, thaw_json


@dataclass(frozen=True)
class TextBlock:
    text: str
    type: Literal["text"] = "text"


@dataclass(frozen=True)
class ArtifactBlock:
    """Provider-neutral 多模态块；字节通过 ArtifactReference 解析。"""

    artifact: ArtifactReference
    alt_text: str | None = None
    type: Literal["artifact"] = "artifact"


@dataclass(frozen=True)
class ThinkingBlock:
    text: str
    signature: str | None = None
    type: Literal["thinking"] = "thinking"


@dataclass(frozen=True)
class ToolCallBlock:
    id: str
    name: str
    arguments: Mapping[str, Any]
    thought_signature: str | None = None
    type: Literal["tool_call"] = "tool_call"

    def __post_init__(self) -> None:
        object.__setattr__(self, "arguments", freeze_json_object(self.arguments))


MessageBlock = TextBlock | ArtifactBlock | ThinkingBlock | ToolCallBlock
UserContent = TextBlock | ArtifactBlock
AssistantContent = TextBlock | ThinkingBlock | ToolCallBlock
ToolResultContent = TextBlock | ArtifactBlock


def content_block_to_dict(block: MessageBlock) -> dict[str, Any]:
    """将 content block 序列化为可 JSON 落盘的 dict。"""
    if isinstance(block, ArtifactBlock):
        return {
            "type": block.type,
            "artifact": block.artifact.content_dict(),
            "alt_text": block.alt_text,
        }
    if isinstance(block, TextBlock):
        return {"text": block.text, "type": block.type}
    if isinstance(block, ThinkingBlock):
        return {"text": block.text, "signature": block.signature, "type": block.type}
    if isinstance(block, ToolCallBlock):
        return {
            "id": block.id,
            "name": block.name,
            "arguments": thaw_json(block.arguments),
            "thought_signature": block.thought_signature,
            "type": block.type,
        }
    raise TypeError(f"不支持的 content block: {type(block)!r}")


def content_block_from_dict(data: dict[str, Any]) -> MessageBlock:
    """从 dict 还原 content block。"""
    if not isinstance(data, dict):
        raise TypeError("content block 必须是 dict")
    block_type = data.get("type")
    if block_type == "text":
        return TextBlock(text=data["text"])
    if block_type == "artifact":
        return ArtifactBlock(
            artifact=artifact_reference_from_dict(data["artifact"]),
            alt_text=(
                str(data["alt_text"]) if data.get("alt_text") is not None else None
            ),
        )
    if block_type == "thinking":
        return ThinkingBlock(
            text=data["text"],
            signature=data.get("signature"),
        )
    if block_type == "tool_call":
        arguments = data.get("arguments")
        if not isinstance(arguments, dict):
            raise TypeError("ToolCallBlock.arguments 必须是 dict")
        return ToolCallBlock(
            id=data["id"],
            name=data["name"],
            arguments=arguments,
            thought_signature=data.get("thought_signature"),
        )
    raise ValueError(f"未知 content block type: {block_type!r}")


def content_blocks_to_list(blocks: Sequence[MessageBlock]) -> list[dict[str, Any]]:
    return [content_block_to_dict(block) for block in blocks]


def content_blocks_from_list(items: list[dict[str, Any]]) -> list[MessageBlock]:
    if not isinstance(items, list):
        raise TypeError("content 必须是 list")
    return [content_block_from_dict(item) for item in items]
