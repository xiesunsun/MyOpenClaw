"""读取当前 Agent 的 direct child 快照。"""

from __future__ import annotations

from typing import Any

from pickel.tools.base import ToolExecutionContext, ToolExecutionResult, tool


@tool(
    name="list_agents",
    description=(
        "List durable direct child agents and their current status immediately. "
        "This does not wait for completion or return a child response."
    ),
    input_schema={
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
    replay_policy="safe",
)
async def list_agents(
    arguments: dict[str, Any], context: ToolExecutionContext
) -> ToolExecutionResult:
    """返回 durable child 的即时状态投影。"""
    del arguments
    control = context.services.delegation
    if control is None:
        return ToolExecutionResult(
            content="当前上下文没有 DelegationControl。",
            is_error=True,
        )
    snapshots = await control.list_child_agents(
        sender_operation_id=context.identity.operation_id or "",
        sender_step_id=context.identity.step_id or "",
        sender_tool_call_id=context.identity.tool_call_id or "",
    )
    payload = [snapshot.to_dict() for snapshot in snapshots]
    return ToolExecutionResult(
        content=f"当前 Session 有 {len(payload)} 个 direct child Agent。",
        structured_content=payload,
    )
