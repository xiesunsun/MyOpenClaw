"""异步创建 durable child Agent 的内置工具。"""

from __future__ import annotations

from typing import Any

from pickel.conversations.agent_message import UserMessage
from pickel.conversations.content_blocks import TextBlock
from pickel.tools.base import ToolExecutionContext, ToolExecutionResult, tool


@tool(
    name="delegate_agent",
    description=(
        "Start a durable child agent for a focused task. The call only accepts "
        "the child Session and initial message; it does not wait for completion."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "description": {
                "type": "string",
                "description": "Short task label for diagnostics.",
            },
            "prompt": {
                "type": "string",
                "description": "Complete initial task for the child agent.",
            },
        },
        "required": ["description", "prompt"],
        "additionalProperties": False,
    },
    output_schema={
        "type": "object",
        "properties": {
            "child_session_id": {"type": "string"},
            "message_id": {"type": "string"},
        },
        "required": ["child_session_id", "message_id"],
        "additionalProperties": False,
    },
    replay_policy="safe",
)
async def delegate_agent(
    arguments: dict[str, Any], context: ToolExecutionContext
) -> ToolExecutionResult:
    control = context.services.delegation
    if control is None:
        return ToolExecutionResult(
            content="当前上下文没有 DelegationControl。",
            is_error=True,
        )
    description = str(arguments["description"])
    prompt = str(arguments["prompt"])
    if not description.strip() or not prompt.strip():
        return ToolExecutionResult(
            content="delegate_agent 的 description 和 prompt 不能为空。",
            is_error=True,
        )
    delegation = await control.start_delegation(
        parent_operation_id=context.identity.operation_id,
        parent_step_id=context.identity.step_id,
        parent_tool_call_id=context.identity.tool_call_id,
        message=UserMessage((TextBlock(prompt),)),
    )
    payload = {
        "child_session_id": delegation.child_session_id,
        "message_id": delegation.initial_message_id,
    }
    return ToolExecutionResult(
        content=(
            "Child 已接受："
            f"session={delegation.child_session_id}, message={delegation.initial_message_id}"
        ),
        structured_content=payload,
    )
