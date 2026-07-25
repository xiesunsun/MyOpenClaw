"""不可拆分对话单元分组与窗口裁剪。

单元：
1. 单独 UserMessage
2. AssistantMessage（无 tool call）
3. AssistantMessage（含 tool calls）+ 紧随的对应 ToolResultMessage 序列
   （按 tool_call_id 匹配，直到下一条 user / 另一条 assistant）

窗口保留最近 N 个单元，禁止拆开 tool call 与其 tool result。
"""

from __future__ import annotations

from pickel.conversations.agent_message import (
    AgentMessage,
    AssistantMessage,
    ToolResultMessage,
    UserMessage,
)
from pickel.conversations.content_blocks import ToolCallContent


def apply_window(
    messages: list[AgentMessage],
    *,
    unit_window: int = 5,
) -> list[AgentMessage]:
    """按不可拆分单元保留最近 unit_window 个单元并展平。"""
    window = max(1, unit_window)
    units = group_message_units(messages)
    kept = units[-window:] if units else []
    result: list[AgentMessage] = []
    for unit in kept:
        result.extend(unit)
    return result


def group_message_units(messages: list[AgentMessage]) -> list[list[AgentMessage]]:
    """将消息序列划分为不可拆分单元。"""
    units: list[list[AgentMessage]] = []
    index = 0
    n = len(messages)

    while index < n:
        message = messages[index]

        if isinstance(message, UserMessage):
            units.append([message])
            index += 1
            continue

        if isinstance(message, AssistantMessage):
            tool_call_ids = {
                block.id
                for block in message.content
                if isinstance(block, ToolCallContent)
            }
            if not tool_call_ids:
                units.append([message])
                index += 1
                continue

            unit: list[AgentMessage] = [message]
            index += 1
            while index < n:
                following = messages[index]
                if isinstance(following, ToolResultMessage):
                    # 匹配本 assistant 的 tool_call_id；亦吞并紧随的 tool 结果以保持原子性
                    unit.append(following)
                    index += 1
                    continue
                break
            units.append(unit)
            continue

        if isinstance(message, ToolResultMessage):
            # 孤立 tool result：单独成单元，避免丢失
            units.append([message])
            index += 1
            continue

        index += 1

    return units
