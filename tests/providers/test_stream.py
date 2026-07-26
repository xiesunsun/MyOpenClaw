"""StreamDelta：provider 增量的统一表示。"""

from __future__ import annotations

import asyncio
from typing import AsyncIterator

import pytest

from pickel.conversations.agent_message import AssistantMessage
from pickel.conversations.content_blocks import TextContent
from pickel.providers.stream import (
    StreamCompleted,
    StreamDelta,
    TextDelta,
    ThinkingDelta,
    ToolCallArgsDelta,
    accumulate,
)


def _message(text: str = "done") -> AssistantMessage:
    return AssistantMessage(content=[TextContent(text=text)])


async def _gen(deltas: list[StreamDelta]) -> AsyncIterator[StreamDelta]:
    for delta in deltas:
        yield delta


def test_四种_delta_都是_StreamDelta():
    assert isinstance(TextDelta(text="a"), StreamDelta)
    assert isinstance(ThinkingDelta(text="a"), StreamDelta)
    assert isinstance(ToolCallArgsDelta(tool_call_id="c1", partial_json="{"), StreamDelta)
    assert isinstance(StreamCompleted(message=_message()), StreamDelta)


def test_accumulate_返回_completed_携带的消息():
    message = _message("hello")
    deltas = [TextDelta(text="hel"), TextDelta(text="lo"), StreamCompleted(message=message)]

    assert asyncio.run(accumulate(_gen(deltas))) is message


def test_accumulate_忽略_completed_之后的内容():
    """StreamCompleted 是终止信号，之后的 delta 不影响结果。"""
    first = _message("first")
    deltas = [StreamCompleted(message=first), TextDelta(text="ignored")]

    assert asyncio.run(accumulate(_gen(deltas))) is first


def test_accumulate_无_completed_时报错():
    """provider 必须以 StreamCompleted 收尾，否则调用方拿不到消息。"""
    with pytest.raises(ValueError, match="StreamCompleted"):
        asyncio.run(accumulate(_gen([TextDelta(text="a")])))


def test_accumulate_空流报错():
    with pytest.raises(ValueError, match="StreamCompleted"):
        asyncio.run(accumulate(_gen([])))


def test_delta_是_frozen():
    delta = TextDelta(text="a")
    with pytest.raises(Exception) as exc:
        delta.text = "b"  # type: ignore[misc]
    assert type(exc.value).__name__ == "FrozenInstanceError"


def test_模块无网络无_provider_依赖():
    """StreamDelta 是纯值对象，不得依赖任何 SDK。"""
    from pathlib import Path

    import pickel.providers.stream as module

    source = Path(module.__file__).read_text(encoding="utf-8")
    assert "anthropic" not in source
    assert "genai" not in source
