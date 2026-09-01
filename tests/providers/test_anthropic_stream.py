"""Anthropic 真流式：SSE 事件翻译与聚合一致性。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from pickel.context.model_context import ModelContext, SystemContent
from pickel.providers.anthropic import AnthropicMessagesProvider
from pickel.providers.stream import (
    StreamCompleted,
    TextDelta,
    ThinkingDelta,
    ToolCallArgsDelta,
    accumulate,
)


def _event(type_: str, **kwargs) -> SimpleNamespace:
    return SimpleNamespace(type=type_, **kwargs)


def _delta_event(delta_type: str, index: int = 0, **fields) -> SimpleNamespace:
    return _event(
        "content_block_delta",
        index=index,
        delta=SimpleNamespace(type=delta_type, **fields),
    )


FINAL_RESPONSE = SimpleNamespace(
    id="msg_1",
    model="claude-jupiter-v1-p",
    content=[
        SimpleNamespace(type="thinking", thinking="想一下", signature="sig-abc"),
        SimpleNamespace(type="text", text="你好"),
        SimpleNamespace(
            type="tool_use", id="call_1", name="echo", input={"text": "hi"}
        ),
    ],
    usage=SimpleNamespace(
        input_tokens=100,
        output_tokens=20,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
    ),
)

EVENTS = [
    _event("message_start"),
    _event(
        "content_block_start",
        index=0,
        content_block=SimpleNamespace(type="thinking"),
    ),
    _delta_event("thinking_delta", index=0, thinking="想"),
    _delta_event("thinking_delta", index=0, thinking="一下"),
    _delta_event("signature_delta", index=0, signature="sig-abc"),
    _event("content_block_stop", index=0),
    _event("content_block_start", index=1, content_block=SimpleNamespace(type="text")),
    _delta_event("text_delta", index=1, text="你"),
    _delta_event("text_delta", index=1, text="好"),
    _event("content_block_stop", index=1),
    _event(
        "content_block_start",
        index=2,
        content_block=SimpleNamespace(type="tool_use", id="call_1", name="echo"),
    ),
    _delta_event("input_json_delta", index=2, partial_json='{"text"'),
    _delta_event("input_json_delta", index=2, partial_json=': "hi"}'),
    _event("content_block_stop", index=2),
    _event("message_stop"),
]


class _FakeStream:
    def __init__(self, events):
        self._events = events

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def __aiter__(self):
        async def _gen():
            for event in self._events:
                yield event

        return _gen()

    async def get_final_message(self):
        return FINAL_RESPONSE


class _FakeMessages:
    def __init__(self, events):
        self._events = events
        self.calls = 0

    def stream(self, **params):
        self.calls += 1
        return _FakeStream(self._events)


def _provider(events=EVENTS) -> AnthropicMessagesProvider:
    provider = AnthropicMessagesProvider.__new__(AnthropicMessagesProvider)
    provider.model = "claude-jupiter-v1-p"
    provider.provider_name = "anthropic"
    provider.max_output_tokens = 1024
    provider.temperature = None
    provider.provider_options = {}
    provider.client = SimpleNamespace(messages=_FakeMessages(events))
    return provider


def _context() -> ModelContext:
    return ModelContext(system=SystemContent.from_text("sys"), messages=[], tools=[])


async def _collect(provider):
    return [
        delta async for delta in provider.stream_prepared(provider.prepare(_context()))
    ]


def test_thinking_增量被翻译():
    deltas = asyncio.run(_collect(_provider()))
    thinking = [d.text for d in deltas if isinstance(d, ThinkingDelta)]

    assert thinking == ["想", "一下"]


def test_文本增量被翻译():
    deltas = asyncio.run(_collect(_provider()))
    texts = [d.text for d in deltas if isinstance(d, TextDelta)]

    assert texts == ["你", "好"]


def test_工具参数增量带上所属_tool_call_id():
    """input_json_delta 事件本身不带 id，须由 content_block_start 记住。"""
    deltas = asyncio.run(_collect(_provider()))
    args = [d for d in deltas if isinstance(d, ToolCallArgsDelta)]

    assert [d.partial_json for d in args] == ['{"text"', ': "hi"}']
    assert {d.tool_call_id for d in args} == {"call_1"}


def test_最后一个_delta_是_completed_且携带完整消息():
    deltas = asyncio.run(_collect(_provider()))

    assert isinstance(deltas[-1], StreamCompleted)
    message = deltas[-1].message
    kinds = [type(block).__name__ for block in message.content]
    assert kinds == ["ThinkingBlock", "TextBlock", "ToolCallBlock"]


def test_thinking_的_signature_来自_sdk_累积而非自行拼装():
    """signature 丢失会让下一轮请求被 provider 拒绝。"""
    deltas = asyncio.run(_collect(_provider()))
    thinking_block = deltas[-1].message.content[0]

    assert thinking_block.signature == "sig-abc"


def test_usage_进入最终消息():
    deltas = asyncio.run(_collect(_provider()))
    usage = deltas[-1].message.metadata.usage

    assert usage.input_tokens == 100
    assert usage.output_tokens == 20


def test_聚合结果与真流式_completed_逐字段相等():
    provider = _provider()

    from_stream = asyncio.run(
        accumulate(provider.stream_prepared(provider.prepare(_context())))
    )
    from_aggregate = asyncio.run(
        accumulate(_provider().stream_prepared(_provider().prepare(_context())))
    )

    assert [type(b).__name__ for b in from_stream.content] == [
        type(b).__name__ for b in from_aggregate.content
    ]
    assert from_stream.content[1].text == from_aggregate.content[1].text
    assert from_stream.content[0].signature == from_aggregate.content[0].signature
    assert (
        from_stream.metadata.usage.input_tokens
        == from_aggregate.metadata.usage.input_tokens
    )
    assert from_stream.metadata.finish_reason == from_aggregate.metadata.finish_reason


def test_prepared_stream_只发起一次请求():
    provider = _provider()

    asyncio.run(accumulate(provider.stream_prepared(provider.prepare(_context()))))

    assert provider.client.messages.calls == 1


def test_未知事件类型被安全忽略():
    """SDK 加新事件类型时不得炸。"""
    events = list(EVENTS)
    events.insert(1, _event("some_future_event", index=0))
    events.insert(2, _delta_event("some_future_delta", index=0))

    deltas = asyncio.run(_collect(_provider(events)))

    assert isinstance(deltas[-1], StreamCompleted)
