"""工具执行结果到持久消息的单一投影。"""

from __future__ import annotations

import copy

from pickel.conversations.agent_message import ToolResultMessage
from pickel.conversations.content_blocks import TextContent, ToolCallContent
from pickel.tools.base import ToolExecutionResult


def build_tool_result_message(
    tool_call: ToolCallContent,
    result: ToolExecutionResult,
) -> ToolResultMessage:
    """只保留模型合同，避免 Runtime 私有诊断意外进入上下文。"""
    content = (
        copy.deepcopy(result.content_blocks)
        if result.content_blocks
        else [TextContent(text=result.content)]
    )
    return ToolResultMessage(
        tool_call_id=tool_call.id,
        tool_name=tool_call.name,
        content=content,
        is_error=result.is_error,
        structured_content=copy.deepcopy(result.structured_content),
    )
