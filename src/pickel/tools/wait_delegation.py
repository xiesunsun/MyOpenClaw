"""有界等待 direct child 终态并读取持久化结果。"""

from __future__ import annotations

from typing import Any

from pickel.conversations.agent_message import agent_message_to_dict
from pickel.tools.base import ToolExecutionContext, ToolExecutionError, tool


@tool(
    name="wait_delegation",
    description=(
        "Wait for a durable direct child agent for a bounded time. Returns its "
        "persisted final assistant response when terminal; timeout does not cancel it."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "child_session_id": {"type": "string"},
            "timeout_seconds": {
                "type": "number",
                "minimum": 0,
                "maximum": 600,
            },
        },
        "required": ["child_session_id", "timeout_seconds"],
        "additionalProperties": False,
    },
    output_schema={
        "type": "object",
        "properties": {
            "timed_out": {"type": "boolean"},
            "agent": {"type": "object"},
            "assistant_message": {"type": ["object", "null"]},
        },
        "required": ["timed_out", "agent", "assistant_message"],
        "additionalProperties": False,
    },
    replay_policy="safe",
)
async def wait_delegation(
    arguments: dict[str, Any], context: ToolExecutionContext
) -> dict[str, object]:
    control = context.services.delegation
    if control is None:
        raise ToolExecutionError("当前上下文没有 DelegationControl。")
    child_session_id = str(arguments["child_session_id"])
    timeout_seconds = float(arguments["timeout_seconds"])
    if not child_session_id.strip():
        raise ToolExecutionError("wait_delegation 的 child_session_id 不能为空。")
    snapshot, assistant, timed_out = await control.wait_delegation(
        sender_operation_id=context.identity.operation_id or "",
        sender_step_id=context.identity.step_id or "",
        sender_tool_call_id=context.identity.tool_call_id or "",
        target_child_session_id=child_session_id,
        timeout_seconds=timeout_seconds,
    )
    payload = {
        "timed_out": timed_out,
        "agent": snapshot.to_dict(),
        "assistant_message": (
            agent_message_to_dict(assistant) if assistant is not None else None
        ),
    }
    return payload
