"""ToolCall 的实际执行器。"""

from __future__ import annotations

import copy

from pickel.observe.records import ErrorInfo
from pickel.operations.agent_run_state import ToolCallState
from pickel.runs.host_calls import HostCallClient, HostCallCompleted, HostCallContext
from pickel.runs.host_call_types import (
    CONFIRMATION_CALL,
    ConfirmationAnswer,
    ConfirmationRequest,
    HostCallSource,
)
from pickel.runtime.runtime_bindings import RuntimeBindings
from pickel.tools.base import (
    ToolExecutionContext,
    ToolExecutionResult,
)
from pickel.conversations.content_blocks import content_blocks_to_list
from pickel.tools.services import ToolServices
from pickel.tools.validation import validate_tool_arguments, validate_tool_result


class ToolExecutionBoundaryError(RuntimeError):
    pass


class ToolCallExecutor:
    """只执行已经跨过持久化 intent 边界的 ToolCall。"""

    def __init__(self, bindings: RuntimeBindings) -> None:
        self._bindings = bindings

    async def execute_tool_call(
        self,
        *,
        tool_call: ToolCallState,
        session_id: str,
        operation_id: str,
        step_id: str,
        step_sequence: int,
        host_calls: HostCallClient | None = None,
    ) -> ToolExecutionResult:
        if tool_call.execution_state != "intent_recorded":
            raise ToolExecutionBoundaryError(
                "只有 intent_recorded ToolCall 才能执行: "
                f"{tool_call.tool_call_id}={tool_call.execution_state}"
            )
        if tool_call.execution_policy == "deny":
            return self._failure(
                tool_call.decision_reason or "工具调用被 Hook 拒绝",
                error_type="ToolDenied",
                error_kind="denied",
            )
        entry = self._bindings.tool_snapshot.get(tool_call.tool_name)
        if entry is None:
            return self._failure(
                f"工具不可用: {tool_call.tool_name}",
                error_type="ToolNotAvailable",
            )
        invalid_arguments = validate_tool_arguments(
            entry.tool,
            tool_call.arguments,
        )
        if invalid_arguments is not None:
            return self._failure(
                f"工具参数不符合 schema：{invalid_arguments}",
                error_type="ToolArgumentsInvalid",
            )
        if tool_call.execution_policy == "confirm":
            accepted = await self._confirm_tool_call(
                host_calls=host_calls,
                entry=entry,
                tool_call=tool_call,
                session_id=session_id,
                operation_id=operation_id,
                step_id=step_id,
                step_sequence=step_sequence,
            )
            if not accepted:
                return self._failure(
                    "工具调用未获得用户确认",
                    error_type="ToolConfirmationDeclined",
                    error_kind="denied",
                )
        context = ToolExecutionContext(
            agent_id=self._bindings.agent_id,
            session_id=session_id,
            workspace_path=self._bindings.workspace_path,
            services=ToolServices(
                workspace_files=self._bindings.tool_services.workspace_files,
                bash=self._bindings.tool_services.bash,
                activation_control=(self._bindings.tool_services.activation_control),
                skill_store=self._bindings.tool_services.skill_store,
                host_calls=host_calls,
            ),
            operation_id=operation_id,
            step_id=step_id,
            step_sequence=step_sequence,
            tool_call_id=tool_call.tool_call_id,
        )
        try:
            result = await entry.tool.execute(tool_call.arguments, context)
        except Exception as exc:  # 工具异常必须变成模型可见结果。
            return ToolExecutionResult(
                content=f"工具 '{tool_call.tool_name}' 执行失败: {exc}",
                is_error=True,
                error=ErrorInfo.from_exception(exc, kind="exception"),
            )
        invalid = validate_tool_result(entry.tool, result)
        if invalid is None:
            return result
        return self._failure(
            f"工具结果不符合 output_schema：{invalid}",
            error_type="ToolResultInvalid",
            metadata={
                "invalid_result": {
                    "content": result.content,
                    "content_blocks": content_blocks_to_list(result.content_blocks),
                    "structured_content": result.structured_content,
                    "is_error": result.is_error,
                    "metadata": copy.deepcopy(result.metadata),
                }
            },
        )

    @staticmethod
    def _failure(
        message: str,
        *,
        error_type: str,
        error_kind: str = "validation",
        metadata: dict | None = None,
    ) -> ToolExecutionResult:
        return ToolExecutionResult(
            content=message,
            is_error=True,
            metadata=dict(metadata or {}),
            error=ErrorInfo(
                kind=error_kind,
                type=error_type,
                message=message,
                retryable=False,
            ),
        )

    @staticmethod
    async def _confirm_tool_call(
        *,
        host_calls: HostCallClient | None,
        entry,
        tool_call: ToolCallState,
        session_id: str,
        operation_id: str,
        step_id: str,
        step_sequence: int,
    ) -> bool:
        if host_calls is None or not host_calls.supports(CONFIRMATION_CALL):
            return False
        outcome = await host_calls.call(
            CONFIRMATION_CALL,
            ConfirmationRequest(
                source=HostCallSource(
                    kind="mcp" if entry.source.value == "mcp" else "tool",
                    name=entry.origin or entry.name,
                    operation=entry.name,
                ),
                title=f"确认执行工具 {entry.name}",
                message=(tool_call.decision_reason or f"工具 {entry.name} 请求执行"),
            ),
            HostCallContext(
                session_id=session_id,
                operation_id=operation_id,
                step_id=step_id,
                step_sequence=step_sequence,
                tool_call_id=tool_call.tool_call_id,
            ),
        )
        return (
            isinstance(outcome, HostCallCompleted)
            and isinstance(outcome.value, ConfirmationAnswer)
            and outcome.value.decision == "accept"
        )
