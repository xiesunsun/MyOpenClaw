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


class _PlainIterator:
    """只有 __aiter__/__anext__ 的异步迭代器——没有 aclose()。"""

    def __init__(self, deltas: list[StreamDelta]) -> None:
        self._remaining = list(deltas)

    def __aiter__(self) -> _PlainIterator:
        return self

    async def __anext__(self) -> StreamDelta:
        if not self._remaining:
            raise StopAsyncIteration
        return self._remaining.pop(0)


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


def test_accumulate_关闭__aiter__派生的迭代器():
    """AsyncIterable 允许 __aiter__ 返回新生成器；外层没有 aclose 时，
    提前退出也必须关掉这个派生的生成器。

    在事件循环内部采样 closed：asyncio.run 收尾的 shutdown_asyncgens()
    会补跑泄漏生成器的 finally，事后断言会假性通过。
    """
    closed: list[str] = []

    class _DerivedIterable:
        """外层无 aclose，__aiter__ 每次派生一个新的 async generator。"""

        def __aiter__(self) -> AsyncGenerator[StreamDelta, None]:
            return self._gen()

        async def _gen(self) -> AsyncGenerator[StreamDelta, None]:
            try:
                yield StreamCompleted(message=_message())
                yield TextDelta(text="never reached")
            finally:
                closed.append("cleanup")

    async def _run() -> list[str]:
        await accumulate(_DerivedIterable())
        return list(closed)

    assert asyncio.run(_run()) == ["cleanup"]


def test_accumulate_接受没有_aclose_的纯_iterator():
    """纯 AsyncIterator 无资源可关，不该因为缺 aclose() 被拒。"""
    message = _message("plain")
    stream = _PlainIterator([TextDelta(text="p"), StreamCompleted(message=message)])

    assert asyncio.run(accumulate(stream)) is message


def test_纯_iterator_无_completed_仍报_ValueError():
    """缺 aclose() 不得把「流里没有 StreamCompleted」盖成 AttributeError。"""
    with pytest.raises(ValueError, match="StreamCompleted"):
        asyncio.run(accumulate(_PlainIterator([TextDelta(text="a")])))


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
    """源码文本级检查：本模块自己不碰渲染层。

    同样不是导入图级隔离——`import pickel.providers.stream` 会经由包的
    eager import 把 rich 一并拉进内存。
    """
    from pathlib import Path

    import pickel.providers.stream as module

    source = Path(module.__file__).read_text(encoding="utf-8")
    assert "rich" not in source
    assert "prompt_toolkit" not in source
