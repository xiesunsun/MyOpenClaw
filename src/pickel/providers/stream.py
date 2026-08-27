"""Provider 增量的统一表示；Delta 只用于实时展示。"""

from __future__ import annotations

from contextlib import aclosing
from dataclasses import dataclass
from typing import AsyncIterable, Mapping

from pickel.conversations.agent_message import AssistantMessage
from pickel.shared.frozen_json import FrozenJSON, freeze_json_object


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
    """终止信号；可靠恢复只消费聚合后的完整响应。"""

    message: AssistantMessage
    provider_response: Mapping[str, FrozenJSON] | None = None
    http_status: int | None = None

    def __post_init__(self) -> None:
        response = self.provider_response or {}
        object.__setattr__(self, "provider_response", freeze_json_object(response))
        if self.http_status is not None and not 100 <= self.http_status <= 599:
            raise ValueError("StreamCompleted.http_status 必须是合法 HTTP 状态码")


StreamDelta = TextDelta | ThinkingDelta | ToolCallArgsDelta | StreamCompleted


async def accumulate(stream: AsyncIterable[StreamDelta]) -> AssistantMessage:
    """兼容 Provider 单元测试：消费到 StreamCompleted，返回 AssistantMessage。"""
    iterator = stream.__aiter__()
    if hasattr(iterator, "aclose"):
        async with aclosing(iterator):  # type: ignore[type-var]
            return await _consume(iterator)
    return await _consume(iterator)


async def _consume(stream: AsyncIterable[StreamDelta]) -> AssistantMessage:
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
