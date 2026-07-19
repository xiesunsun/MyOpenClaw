"""Conversation domain.

持久消息合同：AgentMessage + content blocks。
SessionMessage / ToolCall 仅为 Task 7/8 前的 runtime re-export。
"""

from myopenclaw.conversations.agent_message import (
    AgentMessage,
    AssistantMessage,
    ToolResultMessage,
    UserMessage,
)
from myopenclaw.conversations.content_blocks import (
    ImageContent,
    TextContent,
    ThinkingContent,
    ToolCallContent,
)
from myopenclaw.conversations.message import MessageRole, SessionMessage, ToolCall
from myopenclaw.conversations.session import Session

__all__ = [
    "AgentMessage",
    "AssistantMessage",
    "ImageContent",
    "MessageRole",
    "Session",
    "SessionMessage",
    "TextContent",
    "ThinkingContent",
    "ToolCall",
    "ToolCallContent",
    "ToolResultMessage",
    "UserMessage",
]
