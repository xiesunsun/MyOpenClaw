"""Conversation Tree 的不可变节点及其严格内容 codec。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from pickel.conversations.agent_message import (
    AgentMessage,
    agent_message_from_dict,
    agent_message_to_dict,
)

ContentType = Literal["agent_message", "history_compaction"]


@dataclass(frozen=True)
class HistoryCompaction:
    summary: str
    first_kept_node_id: str | None
    # 跨压缩累积的文件账本；由生成器从被压缩区域的读写工具调用确定性
    # 提取并合并前序压缩节点的账本，随摘要一起回喂下一次压缩。
    read_files: tuple[str, ...] = ()
    modified_files: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.summary:
            raise ValueError("HistoryCompaction.summary 不能为空")
        object.__setattr__(self, "read_files", tuple(self.read_files))
        object.__setattr__(self, "modified_files", tuple(self.modified_files))


ConversationContent = AgentMessage | HistoryCompaction


@dataclass(frozen=True)
class ConversationNode:
    node_id: str
    session_id: str
    parent_node_id: str | None
    content_type: ContentType
    content: ConversationContent
    created_at: datetime

    def __post_init__(self) -> None:
        if not self.node_id or not self.session_id:
            raise ValueError("node_id 和 session_id 不能为空")
        if self.content_type not in ("agent_message", "history_compaction"):
            raise ValueError(f"不支持的 content_type: {self.content_type!r}")
        if self.content_type == "history_compaction" and not isinstance(
            self.content, HistoryCompaction
        ):
            raise TypeError("history_compaction 必须使用 HistoryCompaction")
        if self.content_type == "agent_message" and isinstance(
            self.content, HistoryCompaction
        ):
            raise TypeError("agent_message 必须使用 AgentMessage")

    def content_dict(self) -> dict[str, Any]:
        if self.content_type == "agent_message":
            return agent_message_to_dict(self.content)  # type: ignore[arg-type]
        content = self.content  # type: ignore[assignment]
        payload = {
            "summary": content.summary,
            "first_kept_node_id": content.first_kept_node_id,
        }
        if content.read_files:
            payload["read_files"] = list(content.read_files)
        if content.modified_files:
            payload["modified_files"] = list(content.modified_files)
        return payload

    def content_json(self) -> str:
        return _encode_json(self.content_dict())

    @classmethod
    def from_content_json(
        cls,
        *,
        node_id: str,
        session_id: str,
        parent_node_id: str | None,
        content_type: ContentType,
        content_json: str,
        created_at: datetime,
    ) -> "ConversationNode":
        value = _decode_object(content_json)
        if content_type == "agent_message":
            _validate_agent_message_object(value)
            content: ConversationContent = agent_message_from_dict(value)
        elif content_type == "history_compaction":
            _require_keys(value, {"summary", "first_kept_node_id"})
            if not isinstance(value["summary"], str):
                raise TypeError("summary 必须是字符串")
            first = value["first_kept_node_id"]
            if first is not None and not isinstance(first, str):
                raise TypeError("first_kept_node_id 必须是字符串或 null")
            read_files = _string_tuple(value, "read_files")
            modified_files = _string_tuple(value, "modified_files")
            content = HistoryCompaction(
                value["summary"], first, read_files, modified_files
            )
        else:
            raise ValueError(f"不支持的 content_type: {content_type!r}")
        return cls(
            node_id=node_id,
            session_id=session_id,
            parent_node_id=parent_node_id,
            content_type=content_type,
            content=content,
            created_at=created_at,
        )


def _string_tuple(value: dict[str, Any], key: str) -> tuple[str, ...]:
    """旧节点的解码兼容：字段缺失落空元组，存在时必须是字符串列表。"""
    if key not in value:
        return ()
    items = value[key]
    if not isinstance(items, list) or not all(isinstance(item, str) for item in items):
        raise TypeError(f"{key} 必须是字符串列表")
    return tuple(items)


def _encode_json(value: dict[str, Any]) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise TypeError("内容必须是合法 JSON object") from exc


def _decode_object(value: str) -> dict[str, Any]:
    try:
        decoded = json.loads(value)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("内容必须是合法 JSON object") from exc
    if not isinstance(decoded, dict):
        raise TypeError("内容必须是 JSON object")
    return decoded


def _require_keys(value: dict[str, Any], keys: set[str]) -> None:
    if set(value) != keys:
        missing = sorted(keys - set(value))
        extra = sorted(set(value) - keys)
        raise ValueError(f"JSON 字段不匹配，missing={missing}, extra={extra}")


def _validate_agent_message_object(value: dict[str, Any]) -> None:
    """在交给历史 codec 前拒绝未知的顶层字段。"""
    version = value.get("payload_version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise TypeError("payload_version 必须是整数")
    role = value.get("role")
    if role == "user":
        keys = {"payload_version", "role", "content"}
    elif role == "assistant":
        keys = {"payload_version", "role", "content", "metadata"}
    elif role == "tool":
        keys = {
            "payload_version",
            "role",
            "tool_call_id",
            "tool_name",
            "content",
            "is_error",
        }
        # v2/v3 历史 payload 可能有旧的 structured_content；校验前丢弃，
        # 不恢复为消息字段或第二份结果权威。
        if version in {2, 3} and "structured_content" in value:
            value = dict(value)
            value.pop("structured_content")
    else:
        raise ValueError(f"未知 AgentMessage role: {role!r}")
    _require_keys(value, keys)
    if not isinstance(value["content"], list):
        raise TypeError("AgentMessage.content 必须是 JSON array")
