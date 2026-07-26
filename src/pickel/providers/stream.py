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
from typing import AsyncIterable

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


async def accumulate(stream: AsyncIterable[StreamDelta]) -> AssistantMessage:
    """消费整条流，返回 StreamCompleted 携带的消息。

    async generator 会被正确关闭：本函数取到 StreamCompleted 就提前 return，
    而 `async for` 里的 return 不关闭上游生成器——它的 finally 要等事件循环
    shutdown 才跑，上游握着 provider 的 HTTP 流时这就是连接泄漏。故对带
    aclose() 的上游用 aclosing 显式收尾。

    纯 AsyncIterator（只有 __aiter__/__anext__）同样接受：它没有需要关闭的
    资源，不该因为缺 aclose() 就被拒绝，更不该让 aclosing 的 AttributeError
    盖掉「流里没有 StreamCompleted」这个真正的错误。

    aclose 检查针对 __aiter__() 派生出的迭代器而非外层对象：AsyncIterable
    允许 __aiter__ 返回新的 async generator，此时外层没有 aclose，但派生的
    生成器必须有人关。
    """
    iterator = stream.__aiter__()
    if hasattr(iterator, "aclose"):
        async with aclosing(iterator):  # type: ignore[type-var]
            return await _consume(iterator)
    return await _consume(iterator)


async def _consume(stream: AsyncIterable[StreamDelta]) -> AssistantMessage:
    """迭代到 StreamCompleted 为止；关闭上游由调用方负责。"""
    consumed = 0
    last: StreamDelta | None = None
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
