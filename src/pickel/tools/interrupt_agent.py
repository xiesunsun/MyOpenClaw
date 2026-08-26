"""请求中断当前 Agent 的 direct child。

这是新的模型工具名。旧的 ``cancel_delegation`` 模块仅为迁移旧 Package 保留，
不会由新的内置工具目录公开。
"""

from __future__ import annotations

from typing import Any

from pickel.tools.base import ToolExecutionContext, ToolExecutionResult, tool


@tool(
    name="interrupt_agent",
    description=(
        "Interrupt the current active operation of a direct child agent. "
        "The child Session and its Inbox are preserved for later work."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "child_session_id": {
                "type": "string",
                "description": "Target direct child Session ID.",
            }
        },
        "required": ["child_session_id"],
        "additionalProperties": False,
    },
    output_schema={
        "type": "object",
        "properties": {
            "child_session_id": {"type": "string"},
            "operation_id": {"type": ["string", "null"]},
            "status": {
                "type": "string",
                "enum": ["cancellation_requested", "no_active_operation"],
            },
        },
        "required": ["child_session_id", "operation_id", "status"],
        "additionalProperties": False,
    },
    replay_policy="safe",
)
async def interrupt_agent(
    arguments: dict[str, Any], context: ToolExecutionContext
) -> ToolExecutionResult:
    control = context.services.delegation
    if control is None:
        return ToolExecutionResult(
            content="当前上下文没有 DelegationControl。", is_error=True
        )
    child_session_id = str(arguments.get("child_session_id", ""))
    if not child_session_id.strip():
        return ToolExecutionResult(
            content="interrupt_agent 的 child_session_id 不能为空。", is_error=True
        )
    operation_id = await control.interrupt_agent(
        sender_operation_id=context.identity.operation_id or "",
        sender_step_id=context.identity.step_id or "",
        sender_tool_call_id=context.identity.tool_call_id or "",
        target_child_session_id=child_session_id,
    )
    status = (
        "cancellation_requested" if operation_id is not None else "no_active_operation"
    )
    payload = {
        "child_session_id": child_session_id,
        "operation_id": operation_id,
        "status": status,
    }
    return ToolExecutionResult(
        content=(
            f"Child 中断请求已接受：child_session_id={child_session_id}, "
            f"status={status}"
        ),
        structured_content=payload,
    )
