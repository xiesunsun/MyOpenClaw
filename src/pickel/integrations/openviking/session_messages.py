"""从 Session active path 提取可同步的 AgentMessage 序列。"""

from __future__ import annotations

from pickel.conversations.agent_message import (
    AgentMessage,
    AssistantMessage,
    ToolResultMessage,
    UserMessage,
    agent_message_from_dict,
)
from pickel.conversations.content_blocks import TextContent, ToolCallContent
from pickel.conversations.message import (
    MessageRole,
    SessionMessage,
    ToolCall,
    ToolCallBatch,
    ToolCallResult,
)
from pickel.conversations.session import Session
from pickel.conversations.session_entry import ENTRY_TYPE_MESSAGE


def list_syncable_agent_messages(session: Session) -> list[AgentMessage]:
    """活动路径上 entry_type=message 的 AgentMessage 列表（根 → leaf）。"""
    messages: list[AgentMessage] = []
    for entry in session.active_path():
        if entry.entry_type != ENTRY_TYPE_MESSAGE:
            continue
        messages.append(agent_message_from_dict(entry.payload))
    return messages


def agent_message_plain_text(message: AgentMessage) -> str:
    """拼接消息中的文本块，供测试与简单观测。"""
    texts: list[str] = []
    for block in message.content:
        if isinstance(block, TextContent):
            texts.append(block.text)
    return "".join(texts)


def agent_message_to_session_message(message: AgentMessage) -> SessionMessage:
    """将 AgentMessage 转为 OpenViking mapper 仍可消费的 SessionMessage 视图。"""
    if isinstance(message, UserMessage):
        return SessionMessage(
            role=MessageRole.USER,
            content=agent_message_plain_text(message),
        )
    if isinstance(message, AssistantMessage):
        text = agent_message_plain_text(message)
        tool_calls = [
            block for block in message.content if isinstance(block, ToolCallContent)
        ]
        batch: ToolCallBatch | None = None
        if tool_calls:
            batch = ToolCallBatch(
                batch_id="ov-sync",
                step_index=0,
                calls=[
                    ToolCall(
                        id=call.id,
                        name=call.name,
                        arguments=dict(call.arguments),
                    )
                    for call in tool_calls
                ],
                results=[],
            )
        return SessionMessage(
            role=MessageRole.ASSISTANT,
            content=text,
            tool_call_batch=batch,
        )
    if isinstance(message, ToolResultMessage):
        text = agent_message_plain_text(message)
        # OpenViking 侧用 tool part 表达结果；单条 result 映射为仅含 tool 输出的 assistant 视图。
        return SessionMessage(
            role=MessageRole.ASSISTANT,
            content="",
            tool_call_batch=ToolCallBatch(
                batch_id="ov-tool-result",
                step_index=0,
                calls=[
                    ToolCall(
                        id=message.tool_call_id,
                        name=message.tool_name,
                        arguments={},
                    )
                ],
                results=[
                    ToolCallResult(
                        call_id=message.tool_call_id,
                        content=text,
                        is_error=message.is_error,
                    )
                ],
            ),
        )
    raise TypeError(f"不支持的 AgentMessage 类型: {type(message)!r}")
