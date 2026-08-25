"""Runtime 事件：tagged union，每个时机一个类型。

与 hook 事件的区别：这些是 fire-and-forget 广播，订阅者只读，
不得改写 agent 行为（设计红线 8）。

**加新事件类型时的硬约束**：payload 里的可变对象（dict / list / 非 frozen
dataclass）必须是拷贝，不得与执行路径共享引用。发射点自己负责拷贝，例如
`arguments=dict(tool_call.arguments)`、`replace(result, metadata=dict(result.metadata))`。
共享引用等于把「只读广播」变成控制点：订阅者改一下事件字段，就能改掉随后
真正执行用的参数、或改掉 hook 看到的输入——红线 8 就此失守。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Awaitable, Callable, ClassVar, TypeAlias

from pickel.conversations.agent_message import (
    ToolResultMessage,
    agent_message_to_dict,
)
from pickel.conversations.content_blocks import content_blocks_to_list
from pickel.runtime.agent_run_usage import AgentRunUsage
from pickel.shared.event_envelope import EventEnvelope
from pickel.tools.base import ToolExecutionResult


@dataclass(frozen=True)
class ToolCallSnapshot:
    tool_call_id: str
    tool_name: str
    arguments: dict[str, Any]


def _tool_call_to_dict(tool_call: ToolCallSnapshot) -> dict[str, Any]:
    return {
        "id": tool_call.tool_call_id,
        "name": tool_call.tool_name,
        "arguments": tool_call.arguments,
    }


def _tool_result_to_dict(result: ToolExecutionResult) -> dict[str, Any]:
    return {
        "content": result.content,
        "content_blocks": content_blocks_to_list(result.content_blocks),
        "structured_content": result.structured_content,
        "is_error": result.is_error,
        "metadata": result.metadata,
        "error": asdict(result.error) if result.error is not None else None,
    }


def _usage_to_dict(usage: AgentRunUsage) -> dict[str, Any]:
    return {
        "steps": usage.steps,
        "input_tokens": usage.input_tokens,
        "cache_read_tokens": usage.cache_read_tokens,
        "cache_write_tokens": usage.cache_write_tokens,
        "output_tokens": usage.output_tokens,
        "actual_input_tokens": usage.actual_input_tokens,
        "elapsed_ms": usage.elapsed_ms,
        "hook_injected_chars": usage.hook_injected_chars,
        "model_label": usage.model_label,
    }


@dataclass(frozen=True)
class RuntimeEventBase:
    EVENT_TYPE: ClassVar[str] = ""

    envelope: EventEnvelope = field(default_factory=EventEnvelope)

    def to_dict(self) -> dict[str, Any]:
        envelope = self.envelope
        base = {
            "event_type": self.EVENT_TYPE,
            "event_id": envelope.event_id,
            "session_id": envelope.identity.session_id,
            "operation_id": envelope.identity.operation_id,
            "step_id": envelope.identity.step_id,
            "step_sequence": envelope.identity.step_sequence,
            "tool_call_id": envelope.identity.tool_call_id,
            "message_id": envelope.identity.message_id,
            "event_sequence": envelope.event_sequence,
            "occurred_at": envelope.occurred_at.isoformat(),
        }
        base.update(self._payload())
        return base

    def _payload(self) -> dict[str, Any]:
        return {}


@dataclass(frozen=True)
class AgentRunStarted(RuntimeEventBase):
    EVENT_TYPE: ClassVar[str] = "agent_run_started"

    user_text: str = ""

    def _payload(self) -> dict[str, Any]:
        return {"user_text": self.user_text}


@dataclass(frozen=True)
class ModelStepStarted(RuntimeEventBase):
    EVENT_TYPE: ClassVar[str] = "model_step_started"


@dataclass(frozen=True)
class ToolCallStarted(RuntimeEventBase):
    EVENT_TYPE: ClassVar[str] = "tool_call_started"

    tool_call: ToolCallSnapshot | None = None
    batch_id: str = ""
    call_index: int = 0
    total_calls: int = 0
    tool_source: str | None = None
    tool_origin: str | None = None
    validation: str = "passed"
    hook_action: str | None = None
    confirmation: str = "not_requested"

    def _payload(self) -> dict[str, Any]:
        return {
            "tool_call": _tool_call_to_dict(self.tool_call) if self.tool_call else None,
            "batch_id": self.batch_id,
            "call_index": self.call_index,
            "total_calls": self.total_calls,
            "tool_source": self.tool_source,
            "tool_origin": self.tool_origin,
            "validation": self.validation,
            "hook_action": self.hook_action,
            "confirmation": self.confirmation,
        }


@dataclass(frozen=True)
class ToolCallCompleted(RuntimeEventBase):
    """成功与失败共用；失败读 tool_result.is_error。"""

    EVENT_TYPE: ClassVar[str] = "tool_call_completed"

    tool_call: ToolCallSnapshot | None = None
    tool_result: ToolExecutionResult | None = None
    tool_result_message: ToolResultMessage | None = None
    batch_id: str = ""
    call_index: int = 0
    total_calls: int = 0
    tool_source: str | None = None
    tool_origin: str | None = None
    validation: str = "passed"
    hook_action: str | None = None
    confirmation: str = "not_requested"

    def _payload(self) -> dict[str, Any]:
        return {
            "tool_call": _tool_call_to_dict(self.tool_call) if self.tool_call else None,
            "tool_result": (
                _tool_result_to_dict(self.tool_result) if self.tool_result else None
            ),
            "tool_result_message": (
                agent_message_to_dict(self.tool_result_message)
                if self.tool_result_message
                else None
            ),
            "batch_id": self.batch_id,
            "call_index": self.call_index,
            "total_calls": self.total_calls,
            "tool_source": self.tool_source,
            "tool_origin": self.tool_origin,
            "validation": self.validation,
            "hook_action": self.hook_action,
            "confirmation": self.confirmation,
        }


@dataclass(frozen=True)
class AssistantMessageEvent(RuntimeEventBase):
    EVENT_TYPE: ClassVar[str] = "assistant_message"

    text: str = ""
    usage: AgentRunUsage | None = None

    def _payload(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "usage": _usage_to_dict(self.usage) if self.usage else None,
        }


@dataclass(frozen=True)
class AgentRunCompleted(RuntimeEventBase):
    EVENT_TYPE: ClassVar[str] = "agent_run_completed"

    usage: AgentRunUsage | None = None
    elapsed_ms: int = 0
    outcome: str = "completed"

    def _payload(self) -> dict[str, Any]:
        return {
            "usage": _usage_to_dict(self.usage) if self.usage else None,
            "elapsed_ms": self.elapsed_ms,
            "outcome": self.outcome,
        }


@dataclass(frozen=True)
class AgentRunFailed(RuntimeEventBase):
    EVENT_TYPE: ClassVar[str] = "agent_run_failed"

    error_type: str = ""
    message: str = ""
    traceback_text: str = ""

    def _payload(self) -> dict[str, Any]:
        return {
            "error_type": self.error_type,
            "message": self.message,
            "traceback": self.traceback_text,
        }


@dataclass(frozen=True)
class ThinkingDeltaEvent(RuntimeEventBase):
    EVENT_TYPE: ClassVar[str] = "thinking_delta"

    text: str = ""

    def _payload(self) -> dict[str, Any]:
        return {"text": self.text}


@dataclass(frozen=True)
class TextDeltaEvent(RuntimeEventBase):
    EVENT_TYPE: ClassVar[str] = "text_delta"

    text: str = ""

    def _payload(self) -> dict[str, Any]:
        return {"text": self.text}


@dataclass(frozen=True)
class ToolCallArgsDeltaEvent(RuntimeEventBase):
    """工具参数的增量 JSON；拼完才是合法 JSON，UI 不要中途解析。"""

    EVENT_TYPE: ClassVar[str] = "tool_call_args_delta"

    tool_call_id: str = ""
    partial_json: str = ""

    def _payload(self) -> dict[str, Any]:
        return {
            "tool_call_id": self.tool_call_id,
            "partial_json": self.partial_json,
        }


@dataclass(frozen=True)
class RequestDigestEvent(RuntimeEventBase):
    """本次 generate 实际发出的 Request 摘要(O4,显式非真源)。

    红线:只含长度/名称/条数,不得携带 system、messages、tools 的正文——
    trace 观测侧的白名单以此为前提。
    """

    EVENT_TYPE: ClassVar[str] = "request_digest"

    system_sections: list[dict[str, Any]] = field(default_factory=list)
    tool_names: list[str] = field(default_factory=list)
    message_count: int = 0
    request_chars: int = 0
    hook_injected_chars: int = 0

    def _payload(self) -> dict[str, Any]:
        return {
            "system_sections": [dict(s) for s in self.system_sections],
            "tool_names": list(self.tool_names),
            "message_count": self.message_count,
            "request_chars": self.request_chars,
            "hook_injected_chars": self.hook_injected_chars,
        }


@dataclass(frozen=True)
class AgentRunInterrupted(RuntimeEventBase):
    """用户中断；partial_text 是已生成但未完成的正文。"""

    EVENT_TYPE: ClassVar[str] = "agent_run_interrupted"

    at_step: int = 0
    partial_text: str = ""

    def _payload(self) -> dict[str, Any]:
        return {"at_step": self.at_step, "partial_text": self.partial_text}


RuntimeEventHandler: TypeAlias = Callable[[RuntimeEventBase], Awaitable[None] | None]
