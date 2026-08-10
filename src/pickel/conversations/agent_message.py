"""AgentMessage 联合类型与序列化合同。

message entry 的 payload 即版本化 AgentMessage。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from pickel.conversations.content_blocks import (
    AssistantContent,
    ContentBlock,
    ToolResultContent,
    UserContent,
    content_blocks_from_list,
    content_blocks_to_list,
)

PAYLOAD_VERSION = 2
SUPPORTED_PAYLOAD_VERSIONS = frozenset({1, 2})


@dataclass(frozen=True)
class ModelUsage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    reasoning_tokens: int | None = None
    total_tokens: int | None = None


@dataclass(frozen=True)
class ModelResponseMetadata:
    provider: str
    model: str
    provider_model_version: str | None = None
    provider_response_id: str | None = None
    finish_reason: str | None = None
    finish_message: str | None = None
    elapsed_ms: int | None = None
    usage: ModelUsage | None = None
    # 该次请求的 system+tools 指纹；供 UsageAnchor 判断锚是否仍然适用。
    # None 表示本次升级之前写入的旧 entry（锚保守失效）。
    context_fingerprint: str | None = None
    # before_request hook 对 Request 的改写量（字符）。0 = 无改写；
    # None = 本次升级之前写入的旧 entry。使 /context 预览与实际请求的偏差可发现。
    hook_injected_chars: int | None = None


@dataclass(frozen=True)
class UserMessage:
    content: list[UserContent] = field(default_factory=list)
    role: Literal["user"] = "user"


@dataclass(frozen=True)
class AssistantMessage:
    content: list[AssistantContent] = field(default_factory=list)
    metadata: ModelResponseMetadata | None = None
    role: Literal["assistant"] = "assistant"


@dataclass(frozen=True)
class ToolResultMessage:
    tool_call_id: str
    tool_name: str
    content: list[ToolResultContent] = field(default_factory=list)
    is_error: bool = False
    # 工具返回给模型的结构化数据。运行时诊断仍留在 ToolExecutionResult。
    structured_content: Any | None = None
    role: Literal["tool"] = "tool"


AgentMessage = UserMessage | AssistantMessage | ToolResultMessage


def _model_usage_to_dict(usage: ModelUsage) -> dict[str, Any]:
    return {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cache_read_tokens": usage.cache_read_tokens,
        "cache_write_tokens": usage.cache_write_tokens,
        "reasoning_tokens": usage.reasoning_tokens,
        "total_tokens": usage.total_tokens,
    }


def _model_usage_from_dict(data: dict[str, Any] | None) -> ModelUsage | None:
    if data is None:
        return None
    if not isinstance(data, dict):
        raise TypeError("usage 必须是 dict 或 null")
    return ModelUsage(
        input_tokens=data.get("input_tokens"),
        output_tokens=data.get("output_tokens"),
        cache_read_tokens=data.get("cache_read_tokens"),
        cache_write_tokens=data.get("cache_write_tokens"),
        reasoning_tokens=data.get("reasoning_tokens"),
        total_tokens=data.get("total_tokens"),
    )


def _metadata_to_dict(metadata: ModelResponseMetadata) -> dict[str, Any]:
    return {
        "provider": metadata.provider,
        "model": metadata.model,
        "provider_model_version": metadata.provider_model_version,
        "provider_response_id": metadata.provider_response_id,
        "finish_reason": metadata.finish_reason,
        "finish_message": metadata.finish_message,
        "elapsed_ms": metadata.elapsed_ms,
        "usage": (
            _model_usage_to_dict(metadata.usage) if metadata.usage is not None else None
        ),
        "context_fingerprint": metadata.context_fingerprint,
        "hook_injected_chars": metadata.hook_injected_chars,
    }


def _metadata_from_dict(
    data: dict[str, Any] | None,
) -> ModelResponseMetadata | None:
    if data is None:
        return None
    if not isinstance(data, dict):
        raise TypeError("metadata 必须是 dict 或 null")
    return ModelResponseMetadata(
        provider=data["provider"],
        model=data["model"],
        provider_model_version=data.get("provider_model_version"),
        provider_response_id=data.get("provider_response_id"),
        finish_reason=data.get("finish_reason"),
        finish_message=data.get("finish_message"),
        elapsed_ms=data.get("elapsed_ms"),
        usage=_model_usage_from_dict(data.get("usage")),
        context_fingerprint=data.get("context_fingerprint"),
        hook_injected_chars=data.get("hook_injected_chars"),
    )


def agent_message_to_dict(message: AgentMessage) -> dict[str, Any]:
    """序列化 AgentMessage；顶层含 payload_version 与 role。"""
    if isinstance(message, UserMessage):
        return {
            "payload_version": PAYLOAD_VERSION,
            "role": "user",
            "content": content_blocks_to_list(list(message.content)),
        }
    if isinstance(message, AssistantMessage):
        payload: dict[str, Any] = {
            "payload_version": PAYLOAD_VERSION,
            "role": "assistant",
            "content": content_blocks_to_list(list(message.content)),
            "metadata": (
                _metadata_to_dict(message.metadata)
                if message.metadata is not None
                else None
            ),
        }
        return payload
    if isinstance(message, ToolResultMessage):
        return {
            "payload_version": PAYLOAD_VERSION,
            "role": "tool",
            "tool_call_id": message.tool_call_id,
            "tool_name": message.tool_name,
            "content": content_blocks_to_list(list(message.content)),
            "is_error": message.is_error,
            "structured_content": message.structured_content,
        }
    raise TypeError(f"不支持的 AgentMessage 类型: {type(message)!r}")


def agent_message_from_dict(data: dict[str, Any]) -> AgentMessage:
    """从 dict 还原 AgentMessage。"""
    if not isinstance(data, dict):
        raise TypeError("AgentMessage payload 必须是 dict")

    version = data.get("payload_version")
    if version not in SUPPORTED_PAYLOAD_VERSIONS:
        raise ValueError(
            f"不支持的 payload_version: {version!r}"
            f"（当前支持 {sorted(SUPPORTED_PAYLOAD_VERSIONS)}）"
        )

    role = data.get("role")
    if role == "user":
        blocks = content_blocks_from_list(data.get("content") or [])
        return UserMessage(content=_as_user_content(blocks))
    if role == "assistant":
        blocks = content_blocks_from_list(data.get("content") or [])
        return AssistantMessage(
            content=_as_assistant_content(blocks),
            metadata=_metadata_from_dict(data.get("metadata")),
        )
    if role == "tool":
        blocks = content_blocks_from_list(data.get("content") or [])
        return ToolResultMessage(
            tool_call_id=data["tool_call_id"],
            tool_name=data["tool_name"],
            content=_as_tool_result_content(blocks),
            is_error=bool(data.get("is_error", False)),
            structured_content=(
                data.get("structured_content") if version >= 2 else None
            ),
        )
    raise ValueError(f"未知 AgentMessage role: {role!r}")


def _as_user_content(blocks: list[ContentBlock]) -> list[UserContent]:
    result: list[UserContent] = []
    for block in blocks:
        if not isinstance(block, UserContent):
            raise TypeError(f"UserMessage 不支持 content block: {type(block).__name__}")
        result.append(block)
    return result


def _as_assistant_content(blocks: list[ContentBlock]) -> list[AssistantContent]:
    result: list[AssistantContent] = []
    for block in blocks:
        if not isinstance(block, AssistantContent):
            raise TypeError(
                f"AssistantMessage 不支持 content block: {type(block).__name__}"
            )
        result.append(block)
    return result


def _as_tool_result_content(blocks: list[ContentBlock]) -> list[ToolResultContent]:
    result: list[ToolResultContent] = []
    for block in blocks:
        if not isinstance(block, ToolResultContent):
            raise TypeError(
                f"ToolResultMessage 不支持 content block: {type(block).__name__}"
            )
        result.append(block)
    return result
