"""AgentRunState、ModelStepState 和 ToolCallState 的恢复合同。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Mapping

from pickel.context.model_context import ModelContext, model_context_from_dict
from pickel.shared.frozen_json import freeze_json_object, thaw_json

AgentRunStatus = Literal[
    "queued",
    "running",
    "waiting",
    "cancelling",
    "succeeded",
    "failed",
    "cancelled",
]
WaitingReason = Literal["tool_approval", "tool_reconciliation"]
ModelStepPhase = Literal["preparing_request", "request_ready", "awaiting_tools"]
ToolCallStatus = Literal[
    "waiting_approval",
    "ready",
    "rejected",
    "intent_recorded",
    "completed",
]
ToolReplayPolicy = Literal["safe", "never"]
JSONValue = Any


@dataclass(frozen=True)
class DelegateAgentIntent:
    child_package_version_id: str

    def __post_init__(self) -> None:
        if not self.child_package_version_id:
            raise ValueError("child_package_version_id 不能为空")


ToolExecutionIntent = DelegateAgentIntent

_STATUSES = {
    "queued",
    "running",
    "waiting",
    "cancelling",
    "succeeded",
    "failed",
    "cancelled",
}
_PHASES = {"preparing_request", "request_ready", "awaiting_tools"}
_TOOL_STATUSES = {
    "waiting_approval",
    "ready",
    "rejected",
    "intent_recorded",
    "completed",
}


@dataclass(frozen=True)
class ToolApprovalDecision:
    outcome: Literal["approved", "denied"]
    decided_at: datetime
    actor_id: str | None
    reason: str | None

    def __post_init__(self) -> None:
        if self.outcome not in ("approved", "denied"):
            raise ValueError(f"不支持的 approval outcome: {self.outcome!r}")


@dataclass(frozen=True)
class ToolApproval:
    requested_at: datetime
    requested_by: Literal["tool_policy", "hook"]
    reason: str | None
    decision: ToolApprovalDecision | None

    def __post_init__(self) -> None:
        if self.requested_by not in ("tool_policy", "hook"):
            raise ValueError(f"不支持的 requested_by: {self.requested_by!r}")


@dataclass(frozen=True)
class ModelRequestIntent:
    model_context: ModelContext
    context_fingerprint: str

    def __post_init__(self) -> None:
        if not self.context_fingerprint:
            raise ValueError("context_fingerprint 不能为空")


@dataclass(frozen=True)
class ToolCallState:
    tool_call_id: str
    tool_name: str
    arguments: Mapping[str, JSONValue]
    status: ToolCallStatus
    approval: ToolApproval | None
    replay_policy: ToolReplayPolicy
    execution_intent: DelegateAgentIntent | None
    decision_reason: str | None
    result_node_id: str | None
    is_error: bool | None

    def __post_init__(self) -> None:
        if not self.tool_call_id or not self.tool_name:
            raise ValueError("tool_call_id 和 tool_name 不能为空")
        if self.status not in _TOOL_STATUSES:
            raise ValueError(f"不支持的 ToolCallStatus: {self.status!r}")
        if self.replay_policy not in ("safe", "never"):
            raise ValueError(f"不支持的 replay_policy: {self.replay_policy!r}")
        object.__setattr__(self, "arguments", freeze_json_object(self.arguments))
        if self.status == "completed":
            if self.result_node_id is None or self.is_error is None:
                raise ValueError(
                    "completed ToolCallState 必须有 result_node_id 和 is_error"
                )
        elif self.result_node_id is not None or self.is_error is not None:
            raise ValueError("未完成 ToolCallState 不能有结果字段")
        if self.status == "waiting_approval":
            if self.approval is None or self.approval.decision is not None:
                raise ValueError("waiting_approval 必须有未决 approval")
        if self.status == "rejected":
            if self.execution_intent is not None:
                raise ValueError("rejected 不能有 execution_intent")
            if self.approval is not None:
                if self.approval.decision is None:
                    raise ValueError("rejected 必须有 rejected 原因")
                if self.approval.decision.outcome != "denied":
                    raise ValueError("rejected 必须对应 denied approval")
            elif not self.decision_reason:
                raise ValueError("rejected 必须有 rejected 原因")
        if self.status in {"ready", "intent_recorded"} and self.approval is not None:
            if (
                self.approval.decision is not None
                and self.approval.decision.outcome == "denied"
            ):
                raise ValueError("ready/intent_recorded 不能携带 denied approval")


@dataclass(frozen=True)
class ModelStepState:
    step_id: str
    step_sequence: int
    phase: ModelStepPhase
    request_attempt: int
    request_intent: ModelRequestIntent | None
    assistant_message_node_id: str | None
    tool_calls: tuple[ToolCallState, ...]

    def __post_init__(self) -> None:
        if not self.step_id:
            raise ValueError("step_id 不能为空")
        if self.step_sequence < 1:
            raise ValueError("step_sequence 必须大于 0")
        if self.request_attempt < 0:
            raise ValueError("request_attempt 不能小于 0")
        if self.phase not in _PHASES:
            raise ValueError(f"不支持的 ModelStepPhase: {self.phase!r}")
        if len({call.tool_call_id for call in self.tool_calls}) != len(self.tool_calls):
            raise ValueError("ModelStepState 不能包含重复 tool_call_id")
        if self.phase == "preparing_request":
            _require(not self.request_intent, "preparing_request 不能有 request_intent")
            _require(
                self.assistant_message_node_id is None and not self.tool_calls,
                "preparing_request 不能有响应或工具",
            )
        elif self.phase == "request_ready":
            _require(
                self.request_intent is not None, "request_ready 必须有 request_intent"
            )
            _require(
                self.assistant_message_node_id is None and not self.tool_calls,
                "request_ready 不能有响应或工具",
            )
        else:
            _require(
                self.request_intent is None, "awaiting_tools 不能保留 request_intent"
            )
            _require(
                self.assistant_message_node_id is not None and bool(self.tool_calls),
                "awaiting_tools 必须有响应和工具",
            )

    def content_dict(self) -> dict[str, Any]:
        """返回 Store 持久化使用的完整 JSON object。"""
        return _step_to_dict(self)


@dataclass(frozen=True)
class AgentRunError:
    code: str
    message: str
    retryable: bool

    def __post_init__(self) -> None:
        if not self.code or not self.message:
            raise ValueError("AgentRunError.code 和 message 不能为空")


@dataclass(frozen=True)
class Cancellation:
    cause: str
    requested_at: datetime

    def __post_init__(self) -> None:
        if not self.cause:
            raise ValueError("Cancellation.cause 不能为空")


@dataclass(frozen=True)
class AgentRunState:
    operation_id: str
    revision: int
    status: AgentRunStatus
    waiting_reason: WaitingReason | None
    completed_step_count: int
    current_step: ModelStepState | None
    final_assistant_node_id: str | None
    error: AgentRunError | None
    cancellation: Cancellation | None

    def __post_init__(self) -> None:
        if not self.operation_id:
            raise ValueError("operation_id 不能为空")
        if self.revision < 1 or self.completed_step_count < 0:
            raise ValueError("revision 必须大于 0，completed_step_count 不能小于 0")
        if self.status not in _STATUSES:
            raise ValueError(f"不支持的 AgentRunStatus: {self.status!r}")
        if self.waiting_reason not in (None, "tool_approval", "tool_reconciliation"):
            raise ValueError(f"不支持的 waiting_reason: {self.waiting_reason!r}")
        _require(
            (self.status == "waiting") == (self.waiting_reason is not None),
            "只有 waiting 状态允许有 waiting_reason",
        )
        _require(
            (self.status == "failed") == (self.error is not None),
            "只有 failed 状态必须有 error",
        )
        _require(
            (self.status in {"cancelling", "cancelled"})
            == (self.cancellation is not None),
            "cancelling/cancelled 必须有 cancellation",
        )
        _require(
            (self.status == "succeeded") == (self.final_assistant_node_id is not None),
            "succeeded 必须有 final_assistant_node_id",
        )
        _require(
            self.status not in {"succeeded", "failed", "cancelled"}
            or self.current_step is None,
            "终态 AgentRunState 不能保留 current_step",
        )
        _require(
            self.status != "queued" or self.current_step is None,
            "queued AgentRunState 不能提前创建 current_step",
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation_id": self.operation_id,
            "revision": self.revision,
            "status": self.status,
            "waiting_reason": self.waiting_reason,
            "completed_step_count": self.completed_step_count,
            "current_step": _step_to_dict(self.current_step),
            "final_assistant_node_id": self.final_assistant_node_id,
            "error": _error_to_dict(self.error),
            "cancellation": _cancellation_to_dict(self.cancellation),
        }

    def content_dict(self) -> dict[str, Any]:
        """返回 Store 使用的 JSON object。"""
        return self.to_dict()

    def to_json(self) -> str:
        return json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )

    @classmethod
    def from_json(cls, value: str) -> "AgentRunState":
        try:
            data = json.loads(value)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("AgentRunState 必须是合法 JSON object") from exc
        if not isinstance(data, dict):
            raise TypeError("AgentRunState 必须是 JSON object")
        return agent_run_state_from_content(data)


def agent_run_state_from_content(content: dict[str, Any]) -> AgentRunState:
    if not isinstance(content, dict):
        raise TypeError("AgentRunState 必须是 JSON object")
    _require_keys(content, set(AgentRunState.__dataclass_fields__))
    current = content["current_step"]
    current_step = _step_from_dict(current) if current is not None else None
    error = _error_from_dict(content["error"])
    cancellation = _cancellation_from_dict(content["cancellation"])
    return AgentRunState(
        operation_id=_string(content, "operation_id"),
        revision=_integer(content, "revision"),
        status=_choice(content, "status", _STATUSES),
        waiting_reason=_optional_choice(
            content, "waiting_reason", {"tool_approval", "tool_reconciliation"}
        ),
        completed_step_count=_integer(content, "completed_step_count"),
        current_step=current_step,
        final_assistant_node_id=_optional_string(content, "final_assistant_node_id"),
        error=error,
        cancellation=cancellation,
    )


def _step_to_dict(step: ModelStepState | None) -> dict[str, Any] | None:
    if step is None:
        return None
    return {
        "step_id": step.step_id,
        "step_sequence": step.step_sequence,
        "phase": step.phase,
        "request_attempt": step.request_attempt,
        "request_intent": _intent_to_dict(step.request_intent),
        "assistant_message_node_id": step.assistant_message_node_id,
        "tool_calls": [_tool_to_dict(call) for call in step.tool_calls],
    }


def _step_from_dict(value: Any) -> ModelStepState:
    if not isinstance(value, dict):
        raise TypeError("current_step 必须是 JSON object 或 null")
    _require_keys(value, set(ModelStepState.__dataclass_fields__))
    calls = value["tool_calls"]
    if not isinstance(calls, list):
        raise TypeError("tool_calls 必须是 JSON array")
    return ModelStepState(
        step_id=_string(value, "step_id"),
        step_sequence=_integer(value, "step_sequence"),
        phase=_choice(value, "phase", _PHASES),
        request_attempt=_integer(value, "request_attempt"),
        request_intent=_intent_from_dict(value["request_intent"]),
        assistant_message_node_id=_optional_string(value, "assistant_message_node_id"),
        tool_calls=tuple(_tool_from_dict(item) for item in calls),
    )


def _intent_to_dict(intent: ModelRequestIntent | None) -> dict[str, Any] | None:
    if intent is None:
        return None
    return {
        "model_context": intent.model_context.to_dict(),
        "context_fingerprint": intent.context_fingerprint,
    }


def _intent_from_dict(value: Any) -> ModelRequestIntent | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise TypeError("request_intent 必须是 JSON object 或 null")
    _require_keys(value, {"model_context", "context_fingerprint"})
    context = value["model_context"]
    if not isinstance(context, dict):
        raise TypeError("model_context 必须是 JSON object")
    return ModelRequestIntent(
        model_context=model_context_from_dict(context),
        context_fingerprint=_string(value, "context_fingerprint"),
    )


def _tool_to_dict(call: ToolCallState) -> dict[str, Any]:
    return {
        "tool_call_id": call.tool_call_id,
        "tool_name": call.tool_name,
        "arguments": thaw_json(call.arguments),
        "status": call.status,
        "approval": _approval_to_dict(call.approval),
        "replay_policy": call.replay_policy,
        "execution_intent": _execution_intent_to_dict(call.execution_intent),
        "decision_reason": call.decision_reason,
        "result_node_id": call.result_node_id,
        "is_error": call.is_error,
    }


def _tool_from_dict(value: Any) -> ToolCallState:
    if not isinstance(value, dict):
        raise TypeError("tool_calls 元素必须是 JSON object")
    _require_keys(value, set(ToolCallState.__dataclass_fields__))
    arguments = value["arguments"]
    if not isinstance(arguments, dict):
        raise TypeError("arguments 必须是 JSON object")
    intent = _execution_intent_from_dict(value["execution_intent"])
    return ToolCallState(
        tool_call_id=_string(value, "tool_call_id"),
        tool_name=_string(value, "tool_name"),
        arguments=arguments,
        status=_choice(value, "status", _TOOL_STATUSES),
        approval=_approval_from_dict(value["approval"]),
        replay_policy=_choice(value, "replay_policy", {"safe", "never"}),
        execution_intent=intent,
        decision_reason=_optional_string(value, "decision_reason"),
        result_node_id=_optional_string(value, "result_node_id"),
        is_error=_optional_bool(value, "is_error"),
    )


def _execution_intent_to_dict(
    value: DelegateAgentIntent | None,
) -> dict[str, Any] | None:
    if value is None:
        return None
    return {
        "kind": "delegate_agent",
        "child_package_version_id": value.child_package_version_id,
    }


def _execution_intent_from_dict(value: Any) -> DelegateAgentIntent | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise TypeError("execution_intent 必须是 JSON object 或 null")
    if set(value) != {"kind", "child_package_version_id"}:
        raise ValueError("execution_intent 字段不匹配")
    if value.get("kind") != "delegate_agent":
        raise ValueError(f"未知 execution_intent.kind: {value.get('kind')!r}")
    return DelegateAgentIntent(_string(value, "child_package_version_id"))


def _approval_to_dict(value: ToolApproval | None) -> dict[str, Any] | None:
    if value is None:
        return None
    decision = value.decision
    return {
        "requested_at": value.requested_at.isoformat(),
        "requested_by": value.requested_by,
        "reason": value.reason,
        "decision": (
            {
                "outcome": decision.outcome,
                "decided_at": decision.decided_at.isoformat(),
                "actor_id": decision.actor_id,
                "reason": decision.reason,
            }
            if decision is not None
            else None
        ),
    }


def _approval_from_dict(value: Any) -> ToolApproval | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise TypeError("approval 必须是 JSON object 或 null")
    _require_keys(value, {"requested_at", "requested_by", "reason", "decision"})
    decision_value = value["decision"]
    decision = None
    if decision_value is not None:
        if not isinstance(decision_value, dict):
            raise TypeError("approval.decision 必须是 JSON object 或 null")
        _require_keys(decision_value, {"outcome", "decided_at", "actor_id", "reason"})
        decision = ToolApprovalDecision(
            outcome=_choice(decision_value, "outcome", {"approved", "denied"}),
            decided_at=_time(decision_value, "decided_at"),
            actor_id=_optional_string(decision_value, "actor_id"),
            reason=_optional_string(decision_value, "reason"),
        )
    return ToolApproval(
        requested_at=_time(value, "requested_at"),
        requested_by=_choice(value, "requested_by", {"tool_policy", "hook"}),
        reason=_optional_string(value, "reason"),
        decision=decision,
    )


def _error_to_dict(value: AgentRunError | None) -> dict[str, Any] | None:
    return (
        {"code": value.code, "message": value.message, "retryable": value.retryable}
        if value is not None
        else None
    )


def _error_from_dict(value: Any) -> AgentRunError | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise TypeError("error 必须是 JSON object 或 null")
    _require_keys(value, {"code", "message", "retryable"})
    retryable = value["retryable"]
    if not isinstance(retryable, bool):
        raise TypeError("error.retryable 必须是 bool")
    return AgentRunError(_string(value, "code"), _string(value, "message"), retryable)


def _cancellation_to_dict(value: Cancellation | None) -> dict[str, Any] | None:
    return (
        {"cause": value.cause, "requested_at": value.requested_at.isoformat()}
        if value is not None
        else None
    )


def _cancellation_from_dict(value: Any) -> Cancellation | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise TypeError("cancellation 必须是 JSON object 或 null")
    _require_keys(value, {"cause", "requested_at"})
    return Cancellation(_string(value, "cause"), _time(value, "requested_at"))


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _require_keys(value: dict[str, Any], keys: set[str]) -> None:
    if set(value) != keys:
        raise ValueError(
            f"JSON 字段不匹配，missing={sorted(keys - set(value))}, "
            f"extra={sorted(set(value) - keys)}"
        )


def _string(value: dict[str, Any], key: str) -> str:
    result = value.get(key)
    if not isinstance(result, str):
        raise TypeError(f"{key} 必须是字符串")
    return result


def _optional_string(value: dict[str, Any], key: str) -> str | None:
    result = value.get(key)
    if result is not None and not isinstance(result, str):
        raise TypeError(f"{key} 必须是字符串或 null")
    return result


def _optional_bool(value: dict[str, Any], key: str) -> bool | None:
    result = value.get(key)
    if result is not None and not isinstance(result, bool):
        raise TypeError(f"{key} 必须是 bool 或 null")
    return result


def _integer(value: dict[str, Any], key: str) -> int:
    result = value.get(key)
    if isinstance(result, bool) or not isinstance(result, int):
        raise TypeError(f"{key} 必须是整数")
    return result


def _choice(value: dict[str, Any], key: str, choices: set[str]) -> str:
    result = _string(value, key)
    if result not in choices:
        raise ValueError(f"{key} 值无效: {result!r}")
    return result


def _optional_choice(value: dict[str, Any], key: str, choices: set[str]) -> str | None:
    result = value.get(key)
    if result is None:
        return None
    if not isinstance(result, str) or result not in choices:
        raise ValueError(f"{key} 值无效: {result!r}")
    return result


def _time(value: dict[str, Any], key: str) -> datetime:
    result = value.get(key)
    if not isinstance(result, str):
        raise TypeError(f"{key} 必须是 ISO8601 字符串")
    try:
        return datetime.fromisoformat(result)
    except ValueError as exc:
        raise ValueError(f"{key} 不是合法 ISO8601 时间") from exc
