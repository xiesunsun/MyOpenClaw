"""与界面无关的 Runtime Application 数据模型。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pickel.conversations.agent_message import AssistantMessage, UserMessage
from pickel.runtime.agent_run_usage import AgentRunUsage
from pickel.shared.conversation_mode import ConversationMode


@dataclass(frozen=True)
class RuntimeLaunchRequest:
    """Runtime 进程装配请求；agent_ids=None 表示为全部 Agent 装配。"""

    cwd: Path
    agent_ids: tuple[str, ...] | None = None
    session_id: str | None = None

    def __post_init__(self) -> None:
        if self.session_id is not None and self.agent_ids is not None:
            raise ValueError("按 session 装配 Runtime 时不能同时指定 agent_ids")


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
class McpServerInfo:
    name: str
    status: str
    transport: str
    config_scope: str | None
    protocol_version: str | None
    implementation: str | None
    discovered_tools: int
    active_tools: int
    last_error: str | None


@dataclass(frozen=True)
class McpInspection:
    available: bool
    servers: tuple[McpServerInfo, ...] = ()
    diagnostics: tuple[str, ...] = ()


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
class ConversationRequest:
    """打开一个 Runtime Conversation 所需的界面无关参数。"""

    agent_id: str | None = None
    session_id: str | None = None
    persistence: Literal["persistent", "ephemeral"] = "persistent"
    cwd: Path | None = None
    mode: ConversationMode = "batch"

    def __post_init__(self) -> None:
        if self.session_id is not None and self.agent_id is not None:
            raise ValueError("恢复 session 时不能同时指定 agent_id")
        if self.session_id is not None and self.persistence == "ephemeral":
            raise ValueError("ephemeral Conversation 不能恢复持久化 session")
        if self.persistence not in {"persistent", "ephemeral"}:
            raise ValueError(f"未知 persistence: {self.persistence}")
        if self.mode not in {"interactive", "batch"}:
            raise ValueError(f"未知 mode: {self.mode}")


@dataclass(frozen=True)
class AgentRunRequest:
    """一次 AgentRun 请求；所有 Surface 都提交同一份消息合同。"""

    message: UserMessage


@dataclass(frozen=True)
class RuntimeErrorInfo:
    error_type: str
    message: str


@dataclass(frozen=True)
class AgentRunResult:
    """Runtime Application 的稳定 AgentRun 结果。"""

    status: Literal["completed", "blocked", "failed", "cancelled"]
    session_id: str
    operation_id: str
    message: AssistantMessage | None
    usage: AgentRunUsage | None
    elapsed_ms: int
    error: RuntimeErrorInfo | None = None


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
    source: Literal["preview", "model_request_intent", "model_call"] = "preview"


@dataclass(frozen=True)
class SkillActionResult:
    action: str
    pending_id: str
    diff: str | None = None
    path: Path | None = None


@dataclass(frozen=True)
class ReloadResult:
    conversation: "ConversationRuntime"
    warnings: tuple[str, ...] = ()


class RuntimeApplicationError(Exception):
    """应用接口的稳定错误基类。"""


class ConversationClosedError(RuntimeApplicationError):
    pass


class OperationInProgressError(RuntimeApplicationError):
    pass


if TYPE_CHECKING:
    from pickel.app.conversation_runtime import ConversationRuntime
