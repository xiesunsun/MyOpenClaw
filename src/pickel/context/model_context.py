"""ModelContext 与 ToolDefinition 值对象（query 侧模型输入快照）。

与 tools.base.ToolSpec 字段对齐但类型分离；Assembler 后续从 ToolSpec 映射。
SystemContent.sections 在 v1 保持简单列表，不做复杂合成。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pickel.conversations.agent_message import AgentMessage


@dataclass(frozen=True)
class SystemSection:
    """系统提示中的命名分段。"""

    name: str
    text: str


@dataclass(frozen=True)
class SystemContent:
    """由多个 SystemSection 组成的系统提示。"""

    sections: list[SystemSection] = field(default_factory=list)

    @classmethod
    def from_text(cls, text: str) -> SystemContent:
        """从整段文本构造单 section 的 SystemContent；空串返回空 sections。"""
        if not text:
            return cls(sections=[])
        return cls(sections=[SystemSection(name="system", text=text)])

    def as_text(self) -> str:
        """将非空 section 文本用双换行拼接。"""
        return "\n\n".join(section.text for section in self.sections if section.text)


@dataclass(frozen=True)
class ToolDefinition:
    """面向模型的工具定义（与 ToolSpec 类型分离）。"""

    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(frozen=True)
class ModelContext:
    """组装后的模型输入：system + messages + tools。"""

    system: SystemContent
    messages: list[AgentMessage]
    tools: list[ToolDefinition] = field(default_factory=list)
