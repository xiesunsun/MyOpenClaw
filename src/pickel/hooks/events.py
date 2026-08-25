"""Hook 事件 DTO（只读快照）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from pickel.shared.event_envelope import EventIdentity


@dataclass(frozen=True)
class HookEventBase(EventIdentity):
    """向后兼容的别名基类；身份字段全部来自 EventIdentity。"""


@dataclass(frozen=True)
class UserPromptSubmitEvent(HookEventBase):
    prompt: str = ""
    source: Literal["initial", "steer", "follow_up"] = "initial"
    pending_input_id: str | None = None


@dataclass(frozen=True)
class PreToolUseEvent(HookEventBase):
    tool_name: str = ""
    tool_call_id: str = ""
    arguments: dict[str, Any] = field(default_factory=dict)
    tool_source: str = ""
    tool_origin: str | None = None


@dataclass(frozen=True)
class PostToolUseEvent(HookEventBase):
    tool_name: str = ""
    tool_call_id: str = ""
    arguments: dict[str, Any] = field(default_factory=dict)
    result_content: str = ""
    is_error: bool = False
    tool_source: str = ""
    tool_origin: str | None = None


@dataclass(frozen=True)
class PostToolBatchEvent(HookEventBase):
    outcomes: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class AgentRunEndEvent(HookEventBase):
    reason: str = "completed"
