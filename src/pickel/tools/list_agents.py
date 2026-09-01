"""读取当前 Agent 的 direct child 快照。"""

from __future__ import annotations

from typing import Any

from pickel.tools.base import ToolExecutionContext, ToolExecutionError, tool


@tool(
    name="list_agents",
    description=(
        "Return an immediate snapshot of durable direct child agents. This is "
        "diagnostic only: it does not wait, return a child response, or wake for "
        "completion, and must not be used to poll."
    ),
    input_schema={
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
    output_schema={
        "type": "array",
        "description": "Immediate direct-child snapshots; statuses are not a completion wait mechanism.",
        "items": {
            "type": "object",
            "properties": {
                "child_session_id": {"type": "string"},
                "agent_id": {"type": "string"},
                "status": {
                    "type": "string",
                    "enum": [
                        "ready",
                        "queued",
                        "running",
                        "waiting",
                        "cancelling",
                        "succeeded",
                        "failed",
                        "cancelled",
                        "idle",
                        "archived",
                    ],
                },
                "operation_id": {"type": ["string", "null"]},
                "waiting_reason": {
                    "type": ["string", "null"],
                    "enum": ["tool_approval", "tool_reconciliation", None],
                },
                "completed_step_count": {"type": "integer"},
                "final_assistant_node_id": {"type": ["string", "null"]},
                "error": {"type": ["object", "null"]},
                "updated_at": {"type": ["string", "null"]},
                "phase": {"type": ["string", "null"]},
                "request_attempt": {"type": "integer"},
                "pending_message_count": {"type": "integer"},
            },
            "required": ["child_session_id", "agent_id", "status"],
            "additionalProperties": True,
        },
    },
    replay_policy="safe",
)
async def list_agents(
    arguments: dict[str, Any], context: ToolExecutionContext
) -> list[dict[str, object]]:
    """返回 durable child 的即时状态投影。"""
    del arguments
    control = context.services.delegation
    if control is None:
        raise ToolExecutionError("当前上下文没有 DelegationControl。")
    snapshots = await control.list_child_agents(
        sender_operation_id=context.identity.operation_id or "",
        sender_step_id=context.identity.step_id or "",
        sender_tool_call_id=context.identity.tool_call_id or "",
    )
    payload = [snapshot.to_dict() for snapshot in snapshots]
    return payload
