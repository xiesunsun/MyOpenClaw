"""StreamDelta：provider 增量的统一表示。"""

from __future__ import annotations

import asyncio
import dataclasses
from typing import AsyncGenerator

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


async def _gen(deltas: list[StreamDelta]) -> AsyncGenerator[StreamDelta, None]:
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


def test_四种_delta_都是_frozen():
    deltas: list[StreamDelta] = [
        TextDelta(text="a"),
        ThinkingDelta(text="a"),
        ToolCallArgsDelta(tool_call_id="c1", partial_json="{"),
        StreamCompleted(message=_message()),
    ]

    for delta in deltas:
        field_name = dataclasses.fields(delta)[0].name
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(delta, field_name, "mutated")


def test_accumulate_关闭上游生成器():
    """取到 StreamCompleted 就提前 return，上游的 finally 必须已经跑过。

    否则接真 provider 时，握着 HTTP 流的生成器要等事件循环 shutdown 才清理。
    """
    closed: list[str] = []

    async def upstream() -> AsyncGenerator[StreamDelta, None]:
        try:
            yield StreamCompleted(message=_message())
            yield TextDelta(text="never reached")
        finally:
            closed.append("cleanup")

    asyncio.run(accumulate(upstream()))

    assert closed == ["cleanup"]


def test_stream_源码不出现_SDK_名字():
    """源码文本级检查：本模块自己不提 provider SDK。

    只断言文本，不是导入图级隔离——`pickel.providers.__init__` 仍会 eager
    import 两个 SDK，连这条测试的 import 都会把它们拉进内存。收紧那一层要动
    包的重导出契约，不在本任务范围内。
    """
    from pathlib import Path

    import pickel.providers.stream as module

    source = Path(module.__file__).read_text(encoding="utf-8")
    assert "anthropic" not in source
    assert "genai" not in source


def test_stream_源码不出现_UI_库名字():
    """纯值对象模块不得碰渲染层。"""
    from pathlib import Path

    import pickel.providers.stream as module

    source = Path(module.__file__).read_text(encoding="utf-8")
    assert "rich" not in source
    assert "prompt_toolkit" not in source
