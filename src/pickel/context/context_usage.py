"""Provider-neutral ModelContext 大小投影。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from pickel.context.model_context import ModelContext
from pickel.conversations.agent_message import agent_message_to_dict


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
    total_source: str = "estimated"
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
    message_chars = len(
        json.dumps(
            [agent_message_to_dict(message) for message in context.messages],
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
                    "input_schema": tool.input_schema,
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
    )
    total = sum(category.tokens for category in categories)
    free = max(0, max_input_tokens - total) if max_input_tokens is not None else None
    return ContextUsage(
        model_label=model_label,
        total_tokens=total,
        max_input_tokens=max_input_tokens,
        free_tokens=free,
        categories=categories,
    )


def _tokens(characters: int) -> int:
    return (characters + 3) // 4
