"""异步创建 durable child Agent 的内置工具。"""

from __future__ import annotations

from typing import Any

from pickel.conversations.agent_message import UserMessage
from pickel.conversations.content_blocks import TextBlock
from pickel.tools.base import ToolExecutionContext, ToolExecutionError, tool


@tool(
    name="delegate_agent",
    description=(
        "Start a durable child agent for a focused task and return immediately. "
        "The child runs independently; its terminal result is automatically "
        "delivered to this Parent as a UserMessage and wakes the Parent. Do not "
        "wait or poll with bash sleep, files, or list_agents."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "description": {
                "type": "string",
                "description": "Short task label for diagnostics; it does not control waiting.",
            },
            "prompt": {
                "type": "string",
                "description": "Complete initial task for the child agent, which starts independently.",
            },
            "agent_id": {
                "type": "string",
                "description": "Optional target Agent ID from the frozen delegation allowlist.",
            },
        },
        "required": ["description", "prompt"],
        "additionalProperties": False,
    },
    output_schema={
        "type": "object",
        "properties": {
            "child_session_id": {"type": "string"},
            "message_id": {
                "type": "string",
                "description": "ID of the durable initial UserMessage queued for the child.",
            },
        },
        "required": ["child_session_id", "message_id"],
        "additionalProperties": False,
    },
    replay_policy="safe",
)
async def delegate_agent(
    arguments: dict[str, Any], context: ToolExecutionContext
) -> dict[str, str]:
    control = context.services.delegation
    if control is None:
        raise ToolExecutionError("当前上下文没有 DelegationControl。")
    description = str(arguments["description"])
    prompt = str(arguments["prompt"])
    if not description.strip() or not prompt.strip():
        raise ToolExecutionError("delegate_agent 的 description 和 prompt 不能为空。")
    delegation_kwargs = {
        "parent_operation_id": context.identity.operation_id,
        "parent_step_id": context.identity.step_id,
        "parent_tool_call_id": context.identity.tool_call_id,
        "message": UserMessage((TextBlock(prompt),)),
    }
    if "agent_id" in arguments:
        delegation_kwargs["agent_id"] = str(arguments["agent_id"])
    delegation = await control.start_delegation(
        **delegation_kwargs,
    )
    payload = {
        "child_session_id": delegation.child_session_id,
        "message_id": delegation.initial_message_id,
    }
    return payload
