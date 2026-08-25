"""Provider-neutral、可恢复且深度不可变的 ModelContext。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping

from pickel.conversations.agent_message import (
    AgentMessage,
    AssistantMessage,
    ToolResultMessage,
    UserMessage,
    agent_message_from_dict,
    agent_message_to_dict,
)
from pickel.shared.frozen_json import freeze_json_object, thaw_json


@dataclass(frozen=True)
class SystemSection:
    name: str
    text: str


@dataclass(frozen=True)
class SystemContent:
    sections: tuple[SystemSection, ...] = ()

    def __post_init__(self) -> None:
        sections = tuple(self.sections)
        if not all(isinstance(item, SystemSection) for item in sections):
            raise TypeError("SystemContent.sections 必须是 SystemSection 序列")
        object.__setattr__(self, "sections", sections)

    @classmethod
    def from_text(cls, text: str) -> SystemContent:
        if not text:
            return cls()
        return cls((SystemSection(name="system", text=text),))

    def as_text(self) -> str:
        return "\n\n".join(section.text for section in self.sections if section.text)

    def to_dict(self) -> dict[str, Any]:
        return {
            "sections": [
                {"name": section.name, "text": section.text}
                for section in self.sections
            ]
        }


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    input_schema: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "input_schema", freeze_json_object(self.input_schema))

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": thaw_json(self.input_schema),
        }


@dataclass(frozen=True)
class ModelContext:
    """组装后的模型输入：system + messages + tools。"""

    system: SystemContent
    messages: tuple[AgentMessage, ...]
    tools: tuple[ToolDefinition, ...] = ()

    def __post_init__(self) -> None:
        messages = tuple(self.messages)
        tools = tuple(self.tools)
        if not all(_is_agent_message(message) for message in messages):
            raise TypeError("ModelContext.messages 必须是 AgentMessage 序列")
        if not all(isinstance(tool, ToolDefinition) for tool in tools):
            raise TypeError("ModelContext.tools 必须是 ToolDefinition 序列")
        object.__setattr__(self, "messages", tuple(messages))
        object.__setattr__(self, "tools", tuple(tools))

    def to_dict(self) -> dict[str, Any]:
        return {
            "system": self.system.to_dict(),
            "messages": [agent_message_to_dict(message) for message in self.messages],
            "tools": [tool.to_dict() for tool in self.tools],
        }

    def to_json(self) -> str:
        return json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )

    @classmethod
    def from_json(cls, value: str) -> ModelContext:
        try:
            decoded = json.loads(value)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("ModelContext 必须是合法 JSON object") from exc
        if not isinstance(decoded, dict):
            raise TypeError("ModelContext 必须是 JSON object")
        return model_context_from_dict(decoded)


def model_context_from_dict(value: dict[str, Any]) -> ModelContext:
    _require_keys(value, {"system", "messages", "tools"})
    system_value = value["system"]
    if not isinstance(system_value, dict):
        raise TypeError("system 必须是 JSON object")
    _require_keys(system_value, {"sections"})
    sections = system_value["sections"]
    if not isinstance(sections, list):
        raise TypeError("system.sections 必须是 JSON array")
    parsed_sections = []
    for section in sections:
        if not isinstance(section, dict):
            raise TypeError("system.sections 元素必须是 JSON object")
        _require_keys(section, {"name", "text"})
        parsed_sections.append(
            SystemSection(_string(section, "name"), _string(section, "text"))
        )
    messages = value["messages"]
    tools = value["tools"]
    if not isinstance(messages, list) or not isinstance(tools, list):
        raise TypeError("messages 和 tools 必须是 JSON array")
    parsed_tools = []
    for tool in tools:
        if not isinstance(tool, dict):
            raise TypeError("tools 元素必须是 JSON object")
        _require_keys(tool, {"name", "description", "input_schema"})
        if not isinstance(tool["input_schema"], dict):
            raise TypeError("tool.input_schema 必须是 JSON object")
        parsed_tools.append(
            ToolDefinition(
                _string(tool, "name"),
                _string(tool, "description"),
                tool["input_schema"],
            )
        )
    return ModelContext(
        system=SystemContent(tuple(parsed_sections)),
        messages=tuple(_parse_message(item) for item in messages),
        tools=tuple(parsed_tools),
    )


def model_context_to_dict(value: ModelContext) -> dict[str, Any]:
    if not isinstance(value, ModelContext):
        raise TypeError("value 必须是 ModelContext")
    return value.to_dict()


def model_context_to_json(value: ModelContext) -> str:
    if not isinstance(value, ModelContext):
        raise TypeError("value 必须是 ModelContext")
    return value.to_json()


def _parse_message(value: Any) -> AgentMessage:
    if not isinstance(value, dict):
        raise TypeError("messages 元素必须是 JSON object")
    return agent_message_from_dict(value)


def _is_agent_message(value: Any) -> bool:
    return isinstance(value, (UserMessage, AssistantMessage, ToolResultMessage))


def _require_keys(value: dict[str, Any], keys: set[str]) -> None:
    if set(value) != keys:
        raise ValueError(
            f"JSON 字段不匹配，missing={sorted(keys - set(value))}, "
            f"extra={sorted(set(value) - keys)}"
        )


def _string(value: dict[str, Any], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str):
        raise TypeError(f"{key} 必须是字符串")
    return item
