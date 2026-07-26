"""Provider.stream() 的基类默认实现。"""

from __future__ import annotations

import asyncio

import pytest

from pickel.context.model_context import ModelContext, SystemContent
from pickel.conversations.agent_message import AssistantMessage
from pickel.conversations.content_blocks import TextContent
from pickel.providers.base import Provider
from pickel.providers.stream import StreamCompleted, accumulate
from pickel.shared.model_config import ModelConfig


class _OnlyGenerateProvider(Provider):
    """只实现 generate 的 provider——代表全仓 8 个既有测试桩。"""

    def __init__(self) -> None:
        self.calls = 0

    @classmethod
    def from_config(cls, config: ModelConfig) -> "_OnlyGenerateProvider":
        raise NotImplementedError

    async def generate(self, context: ModelContext) -> AssistantMessage:
        self.calls += 1
        return AssistantMessage(content=[TextContent(text="done")])


def _context() -> ModelContext:
    return ModelContext(system=SystemContent.from_text("sys"), messages=[], tools=[])


async def _collect(provider: Provider) -> list:
    return [delta async for delta in provider.stream(_context())]


def test_默认_stream_产出单个_completed():
    deltas = asyncio.run(_collect(_OnlyGenerateProvider()))

    assert len(deltas) == 1
    assert isinstance(deltas[0], StreamCompleted)


def test_默认_stream_的_completed_携带_generate_的结果():
    provider = _OnlyGenerateProvider()

    deltas = asyncio.run(_collect(provider))

    assert deltas[0].message.content[0].text == "done"
    assert provider.calls == 1


def test_accumulate_默认_stream_等价于直接调_generate():
    provider = _OnlyGenerateProvider()

    streamed = asyncio.run(accumulate(provider.stream(_context())))
    direct = asyncio.run(provider.generate(_context()))

    assert streamed.content[0].text == direct.content[0].text


def test_默认_stream_不吞_generate_的异常():
    class _Exploding(_OnlyGenerateProvider):
        async def generate(self, context: ModelContext) -> AssistantMessage:
            raise RuntimeError("provider down")

    async def _run() -> list:
        return [d async for d in _Exploding().stream(_context())]

    with pytest.raises(RuntimeError, match="^provider down$"):
        asyncio.run(_run())
