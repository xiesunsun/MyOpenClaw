"""Hook 事件 DTO（只读快照）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from pickel.conversations.agent_message import AgentMessage
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


@dataclass(frozen=True)
class BeforeRequestEvent(HookEventBase):
    """请求 Intent 提交前，供 Hook 追加受限 Context 内容。"""

    visible_messages: tuple[AgentMessage, ...] = ()
    recall_messages: tuple[AgentMessage, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "visible_messages", tuple(self.visible_messages))
        object.__setattr__(self, "recall_messages", tuple(self.recall_messages))
