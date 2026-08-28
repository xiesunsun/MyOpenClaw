"""按不可拆分消息单元分组的 Context 辅助函数。"""

from __future__ import annotations

from pickel.conversations.agent_message import AgentMessage, UserMessage


def group_message_units(
    messages: list[AgentMessage],
) -> list[list[AgentMessage]]:
    """以 UserMessage 为单元起点，收纳其后的模型与工具消息。"""
    turns: list[list[AgentMessage]] = []
    for message in messages:
        if isinstance(message, UserMessage) or not turns:
            turns.append([message])
            continue
        turns[-1].append(message)
    return turns
