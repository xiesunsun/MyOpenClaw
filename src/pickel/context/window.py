"""按完整用户轮次执行上下文窗口裁剪。"""

from __future__ import annotations

from pickel.conversations.agent_message import (
    AgentMessage,
    UserMessage,
)


def apply_window(
    messages: list[AgentMessage],
    *,
    turn_window: int = 5,
) -> list[AgentMessage]:
    """保留最近 N 个用户轮次，不拆散轮次内部的 Model/Tool Loop。"""
    window = max(1, turn_window)
    turns = group_conversation_turns(messages)
    kept = turns[-window:] if turns else []
    result: list[AgentMessage] = []
    for turn in kept:
        result.extend(turn)
    return result


def group_conversation_turns(
    messages: list[AgentMessage],
) -> list[list[AgentMessage]]:
    """以 UserMessage 为轮次起点，收纳其后的全部模型与工具消息。"""
    turns: list[list[AgentMessage]] = []
    for message in messages:
        if isinstance(message, UserMessage) or not turns:
            turns.append([message])
            continue
        turns[-1].append(message)
    return turns
