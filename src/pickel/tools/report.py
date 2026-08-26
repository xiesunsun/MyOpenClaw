"""向启动当前 child 的 direct parent 发送自包含报告。"""

from __future__ import annotations

from typing import Any

from pickel.tools.base import ToolExecutionContext, ToolExecutionResult, tool


@tool(
    name="report",
    description=(
        "Report a self-contained result to your direct parent. Reporting does not "
        "end this child turn or mean that the child is complete."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "output": {
                "type": "string",
                "description": "Self-contained actionable report for the direct parent.",
            }
        },
        "required": ["output"],
        "additionalProperties": False,
    },
    output_schema={
        "type": "object",
        "properties": {"message_id": {"type": "string"}},
        "required": ["message_id"],
        "additionalProperties": False,
    },
    replay_policy="safe",
)
async def report(
    arguments: dict[str, Any], context: ToolExecutionContext
) -> ToolExecutionResult:
    control = context.services.delegation
    if control is None:
        return ToolExecutionResult(
            content="当前上下文没有 DelegationControl。",
            is_error=True,
        )
    output = arguments.get("output")
    if not isinstance(output, str) or not output.strip():
        return ToolExecutionResult(
            content="report 的 output 不能为空。",
            is_error=True,
        )
    stored = await control.send_child_report(
        sender_operation_id=context.identity.operation_id or "",
        sender_step_id=context.identity.step_id or "",
        sender_tool_call_id=context.identity.tool_call_id or "",
        output=output,
    )
    return ToolExecutionResult(
        content=f"报告已接受：message={stored.message_id}",
        structured_content={"message_id": stored.message_id},
    )
