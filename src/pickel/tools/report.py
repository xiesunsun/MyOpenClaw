"""向启动当前 child 的 direct parent 发送自包含报告。"""

from __future__ import annotations

from typing import Any

from pickel.tools.base import ToolExecutionContext, ToolExecutionError, tool


@tool(
    name="report",
    description=(
        "Send an intermediate, self-contained report from this Child to its direct "
        "Parent. This is not the terminal result and does not end the child "
        "Operation; Runtime automatically delivers the terminal result separately."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "output": {
                "type": "string",
                "description": "Self-contained actionable intermediate report for the direct Parent.",
            }
        },
        "required": ["output"],
        "additionalProperties": False,
    },
    output_schema={
        "type": "object",
        "properties": {
            "message_id": {
                "type": "string",
                "description": "ID of the durable report UserMessage delivered to the Parent.",
            }
        },
        "required": ["message_id"],
        "additionalProperties": False,
    },
    replay_policy="safe",
)
async def report(
    arguments: dict[str, Any], context: ToolExecutionContext
) -> dict[str, str]:
    control = context.services.delegation
    if control is None:
        raise ToolExecutionError("当前上下文没有 DelegationControl。")
    output = arguments.get("output")
    if not isinstance(output, str) or not output.strip():
        raise ToolExecutionError("report 的 output 不能为空。")
    stored = await control.send_child_report(
        sender_operation_id=context.identity.operation_id or "",
        sender_step_id=context.identity.step_id or "",
        sender_tool_call_id=context.identity.tool_call_id or "",
        output=output,
    )
    return {"message_id": stored.message_id}
