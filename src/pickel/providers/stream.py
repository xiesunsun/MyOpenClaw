"""Provider 增量的统一表示。

async generator 不能有返回值（PEP 525），所以「流结束」这个信号
必须走 yield：最后一个 delta 是 StreamCompleted，携带完整的
AssistantMessage。accumulate() 消费到它为止。

这样 generate() 可以完全由 stream() 实现，而不必再写一份增量拼装。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import AsyncIterator

from pickel.conversations.agent_message import AssistantMessage


@dataclass(frozen=True)
class StreamDelta:
    """provider 流式产出的一个片段。"""


@dataclass(frozen=True)
class TextDelta(StreamDelta):
    text: str = ""


@dataclass(frozen=True)
class ThinkingDelta(StreamDelta):
    text: str = ""


@dataclass(frozen=True)
class ToolCallArgsDelta(StreamDelta):
    """工具参数的增量 JSON 片段；拼完才是合法 JSON。"""

    tool_call_id: str = ""
    partial_json: str = ""


@dataclass(frozen=True)
class StreamCompleted(StreamDelta):
    """终止信号，携带 provider 组装好的完整消息。"""

    message: AssistantMessage | None = None


async def accumulate(stream: AsyncIterator[StreamDelta]) -> AssistantMessage:
    """消费整条流，返回 StreamCompleted 携带的消息。"""
    async for delta in stream:
        if isinstance(delta, StreamCompleted):
            if delta.message is None:
                raise ValueError("StreamCompleted 必须携带 message")
            return delta.message
    raise ValueError("流结束时没有 StreamCompleted")
