"""与界面无关的 Runtime Application 数据模型。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class AgentInfo:
    agent_id: str


@dataclass(frozen=True)
class ModelInfo:
    provider: str
    model: str

    @property
    def model_id(self) -> str:
        return f"{self.provider}/{self.model}"


@dataclass(frozen=True)
class ToolInfo:
    name: str
    source: str
    origin: str | None
    version: str | None


@dataclass(frozen=True)
class PendingSkillInfo:
    pending_id: str
    action: str
    skill_name: str
    agent_id: str


@dataclass(frozen=True)
class RuntimeSnapshot:
    session_id: str
    agent_id: str
    status: str
    message_count: int
    updated_at: datetime
    last_message: str | None
    model_id: str
    thinking: str | None


@dataclass(frozen=True)
class ContextInspection:
    """`/context` 的界面无关数据；usage 类型由 context 层定义。"""

    usage: Any | None
    last_turn: Any | None
    session_total: Any | None
    note: str | None
    turns: int
    tool_calls: int
    compactions: int
    tool_definitions: int


@dataclass(frozen=True)
class SkillActionResult:
    action: str
    pending_id: str
    diff: str | None = None
    path: Path | None = None


@dataclass(frozen=True)
class ReloadResult:
    conversation: "RuntimeConversation"
    warnings: tuple[str, ...] = ()


class RuntimeApplicationError(Exception):
    """应用接口的稳定错误基类。"""


class ConversationClosedError(RuntimeApplicationError):
    pass


class TurnInProgressError(RuntimeApplicationError):
    pass


# 仅供类型检查，避免运行时循环导入。
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pickel.app.runtime import RuntimeConversation
