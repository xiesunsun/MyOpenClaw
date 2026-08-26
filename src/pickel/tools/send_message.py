"""向当前 Agent 的 direct child 追加可靠 followup 的内置工具。"""

from __future__ import annotations

from typing import Any

from pickel.conversations.agent_message import UserMessage
from pickel.conversations.content_blocks import TextBlock
from pickel.tools.base import ToolExecutionContext, ToolExecutionResult, tool


@tool(
    name="send_message",
    description=(
        "Send a durable follow-up message to a direct child agent without waiting."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "child_session_id": {
                "type": "string",
                "description": "Target direct child Session ID.",
            },
            "message": {
                "type": "string",
                "description": "Follow-up message for the child agent.",
            },
        },
        "required": ["child_session_id", "message"],
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
async def send_message(
    arguments: dict[str, Any], context: ToolExecutionContext
) -> ToolExecutionResult:
    control = context.services.delegation
    if control is None:
        return ToolExecutionResult(
            content="当前上下文没有 DelegationControl。",
            is_error=True,
        )
    child_session_id = str(arguments["child_session_id"])
    message = str(arguments["message"])
    if not child_session_id.strip() or not message.strip():
        return ToolExecutionResult(
            content="send_message 的 child_session_id 和 message 不能为空。",
            is_error=True,
        )
    stored = await control.send_parent_followup(
        sender_operation_id=context.identity.operation_id or "",
        sender_step_id=context.identity.step_id or "",
        sender_tool_call_id=context.identity.tool_call_id or "",
        target_child_session_id=child_session_id,
        message=UserMessage((TextBlock(message),)),
    )
    payload = {"message_id": stored.message_id}
    return ToolExecutionResult(
        content=(
            f"消息已持久化为 pending：message={stored.message_id}。"
            "这只表示 child 已可靠接收，不表示已处理或完成。"
        ),
        structured_content=payload,
    )
