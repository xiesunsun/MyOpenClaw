"""AgentRun 的可持久化恢复状态。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal

AgentRunStatus = Literal[
    "queued",
    "running",
    "waiting",
    "succeeded",
    "failed",
    "cancelled",
]
ModelStepPhase = Literal[
    "model_request_ready",
    "model_request_intent_recorded",
    "model_request_retry_scheduled",
    "model_request_completed",
    "tool_calls_ready",
    "tool_calls_running",
    "completed",
]
ToolCallExecutionState = Literal["ready", "intent_recorded", "completed"]
ToolCallExecutionPolicy = Literal["execute", "deny", "confirm"]

_AGENT_RUN_STATUSES = {
    "queued",
    "running",
    "waiting",
    "succeeded",
    "failed",
    "cancelled",
}
_MODEL_STEP_PHASES = {
    "model_request_ready",
    "model_request_intent_recorded",
    "model_request_retry_scheduled",
    "model_request_completed",
    "tool_calls_ready",
    "tool_calls_running",
    "completed",
}
_TOOL_CALL_EXECUTION_STATES = {"ready", "intent_recorded", "completed"}
_TOOL_CALL_EXECUTION_POLICIES = {"execute", "deny", "confirm"}


@dataclass(frozen=True)
class ToolCallState:
    tool_call_id: str
    tool_name: str
    arguments: dict[str, Any]
    execution_state: ToolCallExecutionState
    execution_policy: ToolCallExecutionPolicy = "execute"
    decision_reason: str | None = None
    result_message_node_id: str | None = None
    is_error: bool | None = None

    def __post_init__(self) -> None:
        if self.execution_state not in _TOOL_CALL_EXECUTION_STATES:
            raise ValueError(
                f"不支持的 ToolCallState.execution_state: {self.execution_state}"
            )
        if self.execution_policy not in _TOOL_CALL_EXECUTION_POLICIES:
            raise ValueError(
                f"不支持的 ToolCallState.execution_policy: {self.execution_policy}"
            )
        if self.execution_state == "completed":
            if self.result_message_node_id is None or self.is_error is None:
                raise ValueError("completed ToolCallState 必须引用结果节点和错误标记")
        elif self.result_message_node_id is not None or self.is_error is not None:
            raise ValueError("未完成 ToolCallState 不能提前保存结果节点或错误标记")
        object.__setattr__(self, "arguments", _copy_json_object(self.arguments))


@dataclass(frozen=True)
class ModelStepState:
    step_id: str
    step_sequence: int
    phase: ModelStepPhase
    assistant_message_node_id: str | None = None
    tool_calls: tuple[ToolCallState, ...] = ()
    retry_count: int = 0
    post_tool_batch_hook_completed: bool = False

    def __post_init__(self) -> None:
        if self.step_sequence < 1:
            raise ValueError("step_sequence 必须大于 0")
        if self.retry_count < 0:
            raise ValueError("retry_count 不能小于 0")
        if self.phase not in _MODEL_STEP_PHASES:
            raise ValueError(f"不支持的 ModelStepState.phase: {self.phase}")
        tool_call_ids = [tool_call.tool_call_id for tool_call in self.tool_calls]
        if len(tool_call_ids) != len(set(tool_call_ids)):
            raise ValueError("ModelStepState 不能包含重复 tool_call_id")


@dataclass(frozen=True)
class AgentRunState:
    operation_id: str
    revision: int
    status: AgentRunStatus
    user_message_node_id: str
    current_step: ModelStepState | None = None
    completed_step_ids: tuple[str, ...] = ()
    final_assistant_node_id: str | None = None
    error: dict[str, Any] | None = None
    model_context_feedback: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.revision < 1:
            raise ValueError("AgentRunState.revision 必须大于 0")
        if self.status == "succeeded" and self.final_assistant_node_id is None:
            raise ValueError("succeeded AgentRunState 必须引用最终 Assistant 节点")
        if self.status not in _AGENT_RUN_STATUSES:
            raise ValueError(f"不支持的 AgentRunState.status: {self.status}")
        if len(self.completed_step_ids) != len(set(self.completed_step_ids)):
            raise ValueError("AgentRunState 不能包含重复 completed_step_id")
        if self.status in {"failed", "cancelled"} and self.current_step is not None:
            if any(
                tool_call.execution_state == "ready"
                for tool_call in self.current_step.tool_calls
            ):
                raise ValueError(f"{self.status} AgentRunState 不能留下 ready ToolCall")
        if self.error is not None:
            object.__setattr__(self, "error", _copy_json_object(self.error))

    def content_dict(self) -> dict[str, Any]:
        return {
            "operation_id": self.operation_id,
            "operation_type": "agent_run",
            "revision": self.revision,
            "status": self.status,
            "user_message_node_id": self.user_message_node_id,
            "current_step": (
                _model_step_to_dict(self.current_step)
                if self.current_step is not None
                else None
            ),
            "completed_step_ids": list(self.completed_step_ids),
            "final_assistant_node_id": self.final_assistant_node_id,
            "error": _copy_json_object(self.error) if self.error is not None else None,
            "model_context_feedback": list(self.model_context_feedback),
        }


def agent_run_state_from_content(content: dict[str, Any]) -> AgentRunState:
    if content.get("operation_type") != "agent_run":
        raise ValueError(
            f"不支持的 SessionOperationState: {content.get('operation_type')}"
        )
    current_step_data = content.get("current_step")
    current_step = None
    if current_step_data is not None:
        if not isinstance(current_step_data, dict):
            raise TypeError("current_step 必须是 JSON object 或 null")
        current_step = _model_step_from_dict(current_step_data)
    error = content.get("error")
    if error is not None and not isinstance(error, dict):
        raise TypeError("error 必须是 JSON object 或 null")
    return AgentRunState(
        operation_id=str(content["operation_id"]),
        revision=int(content["revision"]),
        status=str(content["status"]),  # type: ignore[arg-type]
        user_message_node_id=str(content["user_message_node_id"]),
        current_step=current_step,
        completed_step_ids=tuple(
            str(value) for value in content.get("completed_step_ids") or ()
        ),
        final_assistant_node_id=(
            str(content["final_assistant_node_id"])
            if content.get("final_assistant_node_id") is not None
            else None
        ),
        error=error,
        model_context_feedback=tuple(
            str(value) for value in content.get("model_context_feedback") or ()
        ),
    )


def _model_step_to_dict(state: ModelStepState) -> dict[str, Any]:
    return {
        "step_id": state.step_id,
        "step_sequence": state.step_sequence,
        "phase": state.phase,
        "assistant_message_node_id": state.assistant_message_node_id,
        "tool_calls": [
            {
                "tool_call_id": tool_call.tool_call_id,
                "tool_name": tool_call.tool_name,
                "arguments": _copy_json_object(tool_call.arguments),
                "execution_state": tool_call.execution_state,
                "execution_policy": tool_call.execution_policy,
                "decision_reason": tool_call.decision_reason,
                "result_message_node_id": tool_call.result_message_node_id,
                "is_error": tool_call.is_error,
            }
            for tool_call in state.tool_calls
        ],
        "retry_count": state.retry_count,
        "post_tool_batch_hook_completed": state.post_tool_batch_hook_completed,
    }


def _model_step_from_dict(content: dict[str, Any]) -> ModelStepState:
    tool_calls = []
    for value in content.get("tool_calls") or ():
        if not isinstance(value, dict):
            raise TypeError("tool_calls 元素必须是 JSON object")
        arguments = value.get("arguments") or {}
        if not isinstance(arguments, dict):
            raise TypeError("ToolCallState.arguments 必须是 JSON object")
        tool_calls.append(
            ToolCallState(
                tool_call_id=str(value["tool_call_id"]),
                tool_name=str(value["tool_name"]),
                arguments=arguments,
                execution_state=str(value["execution_state"]),  # type: ignore[arg-type]
                execution_policy=str(  # type: ignore[arg-type]
                    value.get("execution_policy") or "execute"
                ),
                decision_reason=(
                    str(value["decision_reason"])
                    if value.get("decision_reason") is not None
                    else None
                ),
                result_message_node_id=(
                    str(value["result_message_node_id"])
                    if value.get("result_message_node_id") is not None
                    else None
                ),
                is_error=(
                    bool(value["is_error"])
                    if value.get("is_error") is not None
                    else None
                ),
            )
        )
    return ModelStepState(
        step_id=str(content["step_id"]),
        step_sequence=int(content["step_sequence"]),
        phase=str(content["phase"]),  # type: ignore[arg-type]
        assistant_message_node_id=(
            str(content["assistant_message_node_id"])
            if content.get("assistant_message_node_id") is not None
            else None
        ),
        tool_calls=tuple(tool_calls),
        retry_count=int(content.get("retry_count") or 0),
        post_tool_batch_hook_completed=bool(
            content.get("post_tool_batch_hook_completed", False)
        ),
    )


def _copy_json_object(value: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError("值必须是 JSON object")
    try:
        copied = json.loads(json.dumps(value, ensure_ascii=False))
    except (TypeError, ValueError) as exc:
        raise TypeError("值必须可 JSON 序列化") from exc
    if not isinstance(copied, dict):
        raise TypeError("值必须是 JSON object")
    return copied
