"""请求取消当前 Agent 的 direct child。"""

from __future__ import annotations

from typing import Any

from pickel.tools.base import ToolExecutionContext, ToolExecutionError, tool


@tool(
    name="cancel_delegation",
    description="Cancel a direct child agent without archiving or deleting its Session.",
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
            "status": {"type": "string"},
        },
        "required": ["child_session_id", "operation_id", "status"],
    },
    replay_policy="safe",
)
async def cancel_delegation(
    arguments: dict[str, Any], context: ToolExecutionContext
) -> dict[str, object]:
    control = context.services.delegation
    if control is None:
        raise ToolExecutionError("当前上下文没有 DelegationControl。")
    child_session_id = str(arguments.get("child_session_id", ""))
    if not child_session_id.strip():
        raise ToolExecutionError("cancel_delegation 的 child_session_id 不能为空。")
    operation_id = await control.cancel_delegation(
        sender_operation_id=context.identity.operation_id or "",
        sender_step_id=context.identity.step_id or "",
        sender_tool_call_id=context.identity.tool_call_id or "",
        target_child_session_id=child_session_id,
    )
    status = (
        "cancellation_requested" if operation_id is not None else "no_active_operation"
    )
    return {
        "child_session_id": child_session_id,
        "operation_id": operation_id,
        "status": status,
    }
