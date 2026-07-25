"""运行时迁移 shim：SessionMessage / ToolCallBatch。

**持久合同**（落盘 / Session entry payload）是 `AgentMessage` + content blocks
（见 `agent_message.py` / `content_blocks.py`），**不是**本模块中的类型。

本模块仅供 Task 7/8 前的 Provider generate 与 ReAct 主路径临时使用：

- `ToolCall`：运行时 tool 调用视图（Gemini thought_signature 仍为 bytes）；
  持久形状请用 `ToolCallContent`（str signature）。
- `ToolCallBatch`：短暂执行视图，**禁止落盘**。
- `SessionMessage`：旧 prompt/runtime 形状，**禁止作为 entry payload**。

迁移结束后应删除本文件中的持久语义残留，由 AgentMessage 统一。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from pickel.conversations.metadata import MessageMetadata


class MessageRole(str, Enum):
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


@dataclass(frozen=True)
class ToolCall:
    """运行时 tool call（非落盘）。持久请用 ToolCallContent。"""

    id: str
    name: str
    arguments: dict[str, object]
    thought_signature: Optional[bytes] = None


@dataclass
class ToolCallResult:
    """运行时 tool 结果（非落盘）。持久请用 ToolResultMessage。"""

    call_id: str
    content: str
    is_error: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolCallBatch:
    """运行时 batch 执行视图；禁止写入 session_entries / payload_json。"""

    batch_id: str
    step_index: int
    calls: list[ToolCall] = field(default_factory=list)
    results: list[ToolCallResult] = field(default_factory=list)


@dataclass
class SessionMessage:
    """旧 runtime/prompt 消息形状；禁止作为持久化合同。

    Task 7/8 完成后由 AgentMessage 替换 generate 主路径。
    """

    role: MessageRole
    content: str = ""
    metadata: Optional[MessageMetadata] = None
    tool_call_batch: Optional[ToolCallBatch] = None
    provider_thinking_blocks: list[dict[str, Any]] | None = None
