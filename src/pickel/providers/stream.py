"""Provider 增量的统一表示。

async generator 不能有返回值（PEP 525），所以「流结束」这个信号
必须走 yield：最后一个 delta 是 StreamCompleted，携带完整的
AssistantMessage。accumulate() 消费到它为止。

这样 generate() 可以完全由 stream() 实现，而不必再写一份增量拼装。

StreamDelta 是 union 别名而非基类（同 content_blocks.ContentBlock 的做法）：
各 delta 扁平、字段全部必填，「StreamCompleted 必须携带消息」由构造期
保证；下游 isinstance 分派也不必为一个可实例化的空基类兜底。
"""

from __future__ import annotations

from contextlib import aclosing
from dataclasses import dataclass
from typing import AsyncGenerator

from pickel.conversations.agent_message import AssistantMessage


@dataclass(frozen=True)
class TextDelta:
    text: str


@dataclass(frozen=True)
class ThinkingDelta:
    text: str


@dataclass(frozen=True)
class ToolCallArgsDelta:
    """工具参数的增量 JSON 片段；拼完才是合法 JSON。"""

    tool_call_id: str
    partial_json: str


@dataclass(frozen=True)
class StreamCompleted:
    """终止信号，携带 provider 组装好的完整消息。"""

    message: AssistantMessage


StreamDelta = TextDelta | ThinkingDelta | ToolCallArgsDelta | StreamCompleted


async def accumulate(stream: AsyncGenerator[StreamDelta, None]) -> AssistantMessage:
    """消费整条流，返回 StreamCompleted 携带的消息。

    参数标注是 AsyncGenerator 而非 AsyncIterator：本函数取到 StreamCompleted
    就提前 return，而 `async for` 里的 return 不关闭上游生成器——它的 finally
    要等事件循环 shutdown 才跑。上游握着 provider 的 HTTP 流时这就是连接泄漏，
    所以这里用 aclosing 显式收尾，而 aclosing 要求对象有 aclose()。
    """
    consumed = 0
    last: StreamDelta | None = None
    async with aclosing(stream):
        async for delta in stream:
            consumed += 1
            last = delta
            if isinstance(delta, StreamCompleted):
                return delta.message
    raise ValueError(
        "流结束时没有 StreamCompleted"
        f"（已消费 {consumed} 个 delta，最后一个是 "
        f"{type(last).__name__ if last is not None else '无'}）"
    )
