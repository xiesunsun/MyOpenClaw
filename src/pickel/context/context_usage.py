"""Provider-neutral ModelContext 大小投影。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Literal, Sequence

from pickel.context.model_context import ModelContext
from pickel.conversations.agent_message import (
    AgentMessage,
    AssistantMessage,
    ModelUsage,
    ToolResultMessage,
)
from pickel.conversations.content_blocks import content_blocks_to_list


@dataclass(frozen=True)
class ContextCategory:
    key: str
    tokens: int
    details: tuple[str, ...] = ()


@dataclass(frozen=True)
class ContextUsage:
    model_label: str
    total_tokens: int
    max_input_tokens: int | None
    free_tokens: int | None
    total_source: Literal["counted", "anchor", "anchor_plus_tail", "estimated"] = (
        "estimated"
    )
    categories: tuple[ContextCategory, ...] = field(default_factory=tuple)


def estimate_context_usage(
    context: ModelContext,
    *,
    model_label: str,
    max_input_tokens: int | None,
) -> ContextUsage:
    behavior_chars = sum(
        len(section.text)
        for section in context.system.sections
        if section.name == "behavior"
    )
    skill_sections = {section.name: section for section in context.system.sections}
    skill_guidance_chars = len(
        skill_sections.get("skills_guidance").text
        if skill_sections.get("skills_guidance") is not None
        else ""
    )
    skill_catalog = skill_sections.get("skills_catalog")
    skill_catalog_text = skill_catalog.text if skill_catalog is not None else ""
    known_sections = {"behavior", "skills_guidance", "skills_catalog"}
    other_system_chars = sum(
        len(section.text)
        for section in context.system.sections
        if section.name not in known_sections
    )
    message_chars = len(
        json.dumps(
            [_message_surface(message) for message in context.messages],
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )
    tool_chars = len(
        json.dumps(
            [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "input_schema": tool.to_dict()["input_schema"],
                }
                for tool in context.tools
            ],
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )
    categories = (
        ContextCategory("behavior", _tokens(behavior_chars)),
        ContextCategory("messages", _tokens(message_chars)),
        ContextCategory("tools", _tokens(tool_chars)),
        ContextCategory("skills_guidance", _tokens(skill_guidance_chars)),
        ContextCategory(
            "skills_catalog",
            _tokens(len(skill_catalog_text)),
            details=tuple(
                line for line in skill_catalog_text.splitlines() if line.startswith("-")
            ),
        ),
        ContextCategory("other", _tokens(other_system_chars)),
    )
    estimated_total = sum(category.tokens for category in categories)
    total, source = _anchored_total(context, estimated_total=estimated_total)
    free = max(0, max_input_tokens - total) if max_input_tokens is not None else None
    return ContextUsage(
        model_label=model_label,
        total_tokens=total,
        max_input_tokens=max_input_tokens,
        free_tokens=free,
        total_source=source,
        categories=categories,
    )


def model_context_fingerprint(context: ModelContext) -> str:
    """返回与 ModelRequestIntent 相同的完整 Context 指纹。"""
    return hashlib.sha256(context.to_json().encode("utf-8")).hexdigest()


def _anchored_total(
    context: ModelContext,
    *,
    estimated_total: int,
) -> tuple[int, str]:
    """优先复用最近一次仍适用于当前前缀的 Provider usage。"""
    for index in range(len(context.messages) - 1, -1, -1):
        message = context.messages[index]
        if not isinstance(message, AssistantMessage) or message.metadata is None:
            continue
        metadata = message.metadata
        usage_tokens = _usage_context_tokens(metadata.provider, metadata.usage)
        if (
            usage_tokens is None
            or metadata.context_fingerprint is None
            or metadata.hook_injected_chars != 0
        ):
            continue
        prefix = ModelContext(
            system=context.system,
            messages=context.messages[:index],
            tools=context.tools,
        )
        if model_context_fingerprint(prefix) != metadata.context_fingerprint:
            continue
        trailing = _estimate_messages(context.messages[index + 1 :])
        return (
            usage_tokens + trailing,
            "anchor" if trailing == 0 else "anchor_plus_tail",
        )
    return estimated_total, "estimated"


def _usage_context_tokens(provider: str, usage: ModelUsage | None) -> int | None:
    if usage is None:
        return None
    if usage.total_tokens is not None:
        total = usage.total_tokens
        # Anthropic 的 input_tokens 不含 cache read/write；当前 Provider
        # 映射的 total_tokens 也只包含 input + output。
        if provider == "anthropic":
            total += usage.cache_read_tokens or 0
            total += usage.cache_write_tokens or 0
        return total if total > 0 else None
    if usage.input_tokens is None:
        return None
    total = usage.input_tokens + (usage.output_tokens or 0)
    if provider == "anthropic":
        total += usage.cache_read_tokens or 0
        total += usage.cache_write_tokens or 0
    return total if total > 0 else None


def _estimate_messages(messages: Sequence[AgentMessage]) -> int:
    if not messages:
        return 0
    characters = len(
        json.dumps(
            [_message_surface(message) for message in messages],
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )
    return _tokens(characters)


def _message_surface(message: AgentMessage) -> dict:
    """只保留 Provider 可见语义，不把持久化 metadata 当成 prompt。"""
    value = {
        "role": message.role,
        "content": content_blocks_to_list(message.content),
    }
    if isinstance(message, ToolResultMessage):
        value.update(
            {
                "tool_call_id": message.tool_call_id,
                "tool_name": message.tool_name,
                "is_error": message.is_error,
            }
        )
    return value


def _tokens(characters: int) -> int:
    return (characters + 3) // 4
