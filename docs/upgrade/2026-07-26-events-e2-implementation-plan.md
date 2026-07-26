# E2 streaming + 中断 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 provider 暴露 token 级增量，runtime 把增量转成事件，UI 边生成边显示；同时让 Ctrl-C 能干净地中断一个 turn 而不破坏 session 的可继续性。

**Architecture:** `Provider.stream()` 产出 `StreamDelta` 序列，最后一个 delta 携带完整 `AssistantMessage`。基类给 `stream()` 一个「调 `generate()` 后一次性 yield」的默认实现，所以既有 provider 与测试桩零改动；anthropic 覆写它得到真流式，并让自己的 `generate()` 反过来由自己的 `stream()` 实现。react 消费 stream 并把 delta 转成 runtime 事件。中断用 asyncio 原生取消，落盘规则保证 session 无悬空 tool_call。

**Tech Stack:** Python 3.12、uv、pytest、anthropic SDK（`messages.stream`）、rich（Live）

## Global Constraints

- runtime 不得 import `rich` 或任何 UI 库
- `seq` 只由 EventBus 分配，发射点不得自行编号
- 事件 payload 里的可变对象必须是拷贝，不得与执行路径共享引用（`runtime_events.py` 模块 docstring 已写死此约束）
- trace 是派生物、只写不读、默认关闭
- Session entry + `metadata.usage` 是对话事实的唯一真源
- 「实际输入规模」= `input_tokens + cache_read_tokens + cache_write_tokens`
- **工具快照在 turn 开头取一次**（`react.py:69` 的 `turn.tool_snapshot`），streaming 循环内不得重取——每 step 重取会让 prompt cache 失效
- 任何复现 `/context` 的路径必须给 `prepare()` 传 `snapshot=`，否则 usage 锚失效
- `strategy.execute` 的 `bus` / `turn_id` 签名不变
- `chat.py` 只碰渲染订阅段，不碰装配与 `/reload`（那是工具总线线程的区域）
- TDD：每个任务先写红灯测试，确认失败后再实现
- 测试命令统一带 `GEMINI_API_KEY=fake` 前缀，用 `uv run --with pytest pytest`
- 每个任务提交前 `git checkout uv.lock`

**基线：** 分支起点 `7b97e14`，全仓 `448 passed, 1 skipped, 6 failed`。那 6 个在 `tests/tools/test_shell.py`，是本机 bash bracketed-paste 环境问题，与本计划无关，全程应不增不减。

---

## 关键设计决策：为什么偏离设计稿 §6 的字面表述

设计稿写「`generate()` 必须由 `stream()` 实现」，理由是*两条独立代码路径必然漂移*。**本计划改为反向的默认实现 + 真流式 provider 的自约束**，理由如下：

全仓有 8 个测试桩实现了 `generate()`（`tests/runs/test_react_checkpoint.py`、`test_turn_events.py`、`test_react_observability_metadata.py`、`test_events.py`、`test_runner.py`、`tests/hooks/test_lifecycle_hooks.py`），其中 4 个显式继承 `Provider`。若把 `generate()` 变成基类具体实现、强制子类只实现 `stream()`，这 8 个桩全部要改，而它们测的都不是 streaming。

设计稿那句话的**真实意图是「不要有两份解析逻辑」**，不是「必须由哪个方法调哪个方法」。本计划的形态同样满足这个意图：

| provider | `stream()` | `generate()` | 解析逻辑 |
|---|---|---|---|
| 基类默认 | 调 `generate()`，yield 一次 `StreamCompleted` | 子类实现 | 唯一（在 `generate` 里） |
| anthropic | 覆写，真流式 | 由自己的 `stream()` 实现 | 唯一（在 `stream` 里） |
| gemini | 不覆写，走默认 | 保持现状 | 唯一（在 `generate` 里） |

代价是基类有两个可覆写点，后来者可能写出两条路径。**用 Task 3 的契约测试守住**：anthropic 的 `generate()` 与 `accumulate(stream())` 必须逐字段相等。

**gemini 本轮不做真流式**，理由有二：一是本机没有真实 gemini key（一直用 `GEMINI_API_KEY=fake`），无法真机验证增量聚合；二是 `gemini.py:335` 的 `_extract_text` 用 `"\n".join(texts)` 拼接多个 text part——非流式通常只有 1 个 part，流式每个 chunk 一个 part，简单累积后再 join 会在每个 chunk 之间插入换行，产出与非流式不同的文本。这个聚合要写对必须有真机验证。gemini 走默认 `stream()`，行为与今天逐字节相同。

---

## 文件地图

| 文件 | 责任 | 动作 |
|---|---|---|
| `src/pickel/providers/stream.py` | `StreamDelta` 值对象 + `accumulate()` | 新建 |
| `src/pickel/providers/base.py` | `stream()` 默认实现 | 改 |
| `src/pickel/providers/anthropic.py` | 真流式 + `generate` 由 `stream` 实现 | 改 |
| `src/pickel/runs/runtime_events.py` | 3 个 delta 事件 + `TurnInterrupted` | 改 |
| `src/pickel/runs/strategy/react.py` | 消费 stream、发 delta 事件、中断落盘 | 改 |
| `src/pickel/runs/run.py` | `TurnInterrupted` 事件 | 改 |
| `src/pickel/cli/event_renderer.py` | delta 的 Live 渲染 | 改 |
| `src/pickel/cli/chat.py` | 中断捕获（仅渲染订阅段） | 改 |

---

## Task 1: StreamDelta 值对象

**Files:**
- Create: `src/pickel/providers/stream.py`
- Test: `tests/providers/test_stream.py`

**Interfaces:**
- Consumes: `AssistantMessage`（`pickel.conversations.agent_message`）
- Produces（**以 Task 1 实际落地为准**，与下方 Step 3 的初稿代码有出入）：四个扁平 frozen dataclass `TextDelta(text: str)` / `ThinkingDelta(text: str)` / `ToolCallArgsDelta(tool_call_id: str, partial_json: str)` / `StreamCompleted(message: AssistantMessage)`，字段全部必填；类型别名 `StreamDelta = TextDelta | ThinkingDelta | ToolCallArgsDelta | StreamCompleted`（与 `conversations/content_blocks.py:43-46` 的 `ContentBlock` 同构）；`async def accumulate(stream) -> AssistantMessage`

**审阅期修订的三点，后续任务必须按实际实现来：**

1. **`StreamDelta` 是 union 别名，不是基类。** `isinstance(x, StreamDelta)` 可用（Python 3.12 实测），但 `case StreamDelta()` 的结构化模式匹配**不可用**（`TypeError: called match pattern must be a class`）——UI 渲染器要分派只能 `case TextDelta() | ThinkingDelta() | ...` 列具体类。这与仓库既有的 `ContentBlock` 约束相同。
2. **`accumulate()` 会关闭上游 async generator**（`contextlib.aclosing`），同时兼容纯 `AsyncIterator`。所以 `stream()` 标注成 `AsyncIterator[StreamDelta]` 是合法的——它的实现体含 `yield`，实际返回 async generator，而 `AsyncGenerator` 是 `AsyncIterator` 的子类型。下面 Task 2/3 的标注不必改。
3. 字段无默认值，构造时必须给全。

**为什么最后一个 delta 携带完整消息：** Python 的 async generator 不能有返回值（PEP 525），所以完成信号必须走 yield。`accumulate()` 消费到 `StreamCompleted` 取它的 message——这样 `generate()` 可以完全由 `stream()` 实现而不必自己拼装增量。

- [ ] **Step 1: 写失败测试**

```python
# tests/providers/test_stream.py
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
```

- [ ] **Step 2: 跑测试确认失败**

Run: `GEMINI_API_KEY=fake uv run --with pytest pytest tests/providers/test_stream.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'pickel.providers.stream'`

若 `tests/providers/` 目录不存在则创建，并按项目惯例加 `__init__.py`（参照 `tests/runs/__init__.py`）。

- [ ] **Step 3: 实现**

```python
# src/pickel/providers/stream.py
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
```

- [ ] **Step 4: 跑测试确认通过**

Run: `GEMINI_API_KEY=fake uv run --with pytest pytest tests/providers/test_stream.py -q`
Expected: PASS（7 个）

- [ ] **Step 5: 提交**

```bash
git checkout uv.lock
git add src/pickel/providers/stream.py tests/providers/
git commit -m "feat(stream): StreamDelta 值对象与 accumulate"
```

---

## Task 2: Provider.stream() 默认实现

**Files:**
- Modify: `src/pickel/providers/base.py`
- Test: `tests/providers/test_base_stream.py`

**Interfaces:**
- Consumes: `StreamDelta` / `StreamCompleted`（Task 1）
- Produces: `Provider.stream(context: ModelContext) -> AsyncIterator[StreamDelta]`——基类默认实现，调 `self.generate(context)` 后 yield 一个 `StreamCompleted`

**为什么默认实现是这个方向：** 见本计划开头「关键设计决策」。8 个既有测试桩只实现了 `generate()`，默认实现让它们零改动即可被 streaming 消费方使用。

- [ ] **Step 1: 写失败测试**

```python
# tests/providers/test_base_stream.py
"""Provider.stream() 的基类默认实现。"""

from __future__ import annotations

import asyncio

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


async def _collect(provider: Provider):
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
        async def generate(self, context):
            raise RuntimeError("provider down")

    async def _run():
        return [d async for d in _Exploding().stream(_context())]

    try:
        asyncio.run(_run())
    except RuntimeError as exc:
        assert str(exc) == "provider down"
    else:
        raise AssertionError("异常必须传播给调用方")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `GEMINI_API_KEY=fake uv run --with pytest pytest tests/providers/test_base_stream.py -q`
Expected: FAIL — `AttributeError: '_OnlyGenerateProvider' object has no attribute 'stream'`

- [ ] **Step 3: 实现**

`src/pickel/providers/base.py` 改为：

```python
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, AsyncIterator

from pickel.context.model_context import ModelContext
from pickel.conversations.agent_message import AssistantMessage
from pickel.providers.stream import StreamCompleted, StreamDelta

if TYPE_CHECKING:
    from pickel.shared.model_config import ModelConfig


class Provider(ABC):
    @classmethod
    @abstractmethod
    def from_config(cls, config: "ModelConfig") -> "Provider":
        raise NotImplementedError

    @abstractmethod
    async def generate(self, context: ModelContext) -> AssistantMessage:
        """消费 ModelContext，返回统一 AssistantMessage。"""
        raise NotImplementedError

    async def stream(
        self, context: ModelContext
    ) -> AsyncIterator[StreamDelta]:
        """产出增量；默认实现不流式，一次性给出完整结果。

        覆写此方法即可获得真流式。覆写者必须让自己的 generate()
        由自己的 stream() 实现（`accumulate(self.stream(ctx))`），
        否则同一个 provider 会有两份解析逻辑，迟早漂移。
        """
        yield StreamCompleted(message=await self.generate(context))

    async def count_context_tokens(self, context: ModelContext) -> int | None:
        """统计上下文 token；失败返回 None。"""
        return None
```

- [ ] **Step 4: 跑测试确认通过**

Run: `GEMINI_API_KEY=fake uv run --with pytest pytest tests/providers/ tests/runs/ -q`
Expected: PASS——既有 8 个测试桩不受影响

- [ ] **Step 5: 提交**

```bash
git checkout uv.lock
git add src/pickel/providers/base.py tests/providers/test_base_stream.py
git commit -m "feat(stream): Provider.stream 默认实现，既有 provider 零改动"
```

---

## Task 3: anthropic 真流式

**Files:**
- Modify: `src/pickel/providers/anthropic.py:57-65`
- Test: `tests/providers/test_anthropic_stream.py`

**Interfaces:**
- Consumes: `StreamDelta` 家族（Task 1）、`Provider.stream`（Task 2）
- Produces: `AnthropicProvider.stream()` 真流式；`AnthropicProvider.generate()` 由 `accumulate(self.stream(...))` 实现

**SDK 事实（已核实，不要凭记忆改）：**

- `async with client.messages.stream(**params) as stream:` 进入流
- `async for event in stream:` 吐 SSE 事件
- `event.type == "content_block_delta"` 时，`event.delta.type` 为 `text_delta` / `thinking_delta` / `input_json_delta` / `signature_delta`
- 文本在 `event.delta.text`，思考在 `event.delta.thinking`，工具参数在 `event.delta.partial_json`
- `await stream.get_final_message()` 返回 SDK 累积好的完整消息——**thinking 的 signature 由 SDK 负责拼装，不要自己拼**
- 消费完 `async for` 之后仍可调 `get_final_message()`

**现有代码的复用点：** `anthropic.py:249` 的 `_response_to_assistant_message(response)` 已能把最终响应对象转成 `AssistantMessage`。`get_final_message()` 返回的正是那种对象，所以这一步零重复。

**`content_block_start` 的作用：** `input_json_delta` 事件本身不带 tool_call id，需要在 `content_block_start` 时记下当前 block 的 id。用一个局部 dict 按 `event.index` 映射。

- [ ] **Step 1: 写失败测试**

```python
# tests/providers/test_anthropic_stream.py
"""anthropic 真流式：SSE 事件翻译与 generate/stream 的一致性。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from pickel.context.model_context import ModelContext, SystemContent
from pickel.providers.anthropic import AnthropicProvider
from pickel.providers.stream import (
    StreamCompleted,
    TextDelta,
    ThinkingDelta,
    ToolCallArgsDelta,
    accumulate,
)
from pickel.shared.model_config import ModelConfig


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


def _provider(events=EVENTS) -> AnthropicProvider:
    provider = AnthropicProvider.__new__(AnthropicProvider)
    provider.model = "claude-jupiter-v1-p"
    provider.max_output_tokens = 1024
    provider.temperature = None
    provider.provider_options = {}
    provider.client = SimpleNamespace(messages=_FakeMessages(events))
    return provider


def _context() -> ModelContext:
    return ModelContext(system=SystemContent.from_text("sys"), messages=[], tools=[])


async def _collect(provider):
    return [delta async for delta in provider.stream(_context())]


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
    assert kinds == ["ThinkingContent", "TextContent", "ToolCallContent"]


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


def test_generate_与_accumulate_stream_逐字段相等():
    """契约：真流式 provider 的 generate 必须由自己的 stream 实现。

    两条独立解析路径必然漂移——这条测试是唯一的护栏。
    """
    provider = _provider()

    from_generate = asyncio.run(provider.generate(_context()))
    from_stream = asyncio.run(accumulate(_provider().stream(_context())))

    assert [type(b).__name__ for b in from_generate.content] == [
        type(b).__name__ for b in from_stream.content
    ]
    assert from_generate.content[1].text == from_stream.content[1].text
    assert from_generate.content[0].signature == from_stream.content[0].signature
    assert (
        from_generate.metadata.usage.input_tokens
        == from_stream.metadata.usage.input_tokens
    )
    assert from_generate.metadata.finish_reason == from_stream.metadata.finish_reason


def test_generate_只发起一次请求():
    """generate 由 stream 实现，不得变成两次 API 调用。"""
    provider = _provider()

    asyncio.run(provider.generate(_context()))

    assert provider.client.messages.calls == 1


def test_未知事件类型被安全忽略():
    """SDK 加新事件类型时不得炸。"""
    events = list(EVENTS)
    events.insert(1, _event("some_future_event", index=0))
    events.insert(2, _delta_event("some_future_delta", index=0))

    deltas = asyncio.run(_collect(_provider(events)))

    assert isinstance(deltas[-1], StreamCompleted)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `GEMINI_API_KEY=fake uv run --with pytest pytest tests/providers/test_anthropic_stream.py -q`
Expected: FAIL——当前 `stream()` 是基类默认实现，只产出一个 `StreamCompleted`，没有 delta

- [ ] **Step 3: 实现**

把 `anthropic.py:57-65` 的 `generate` 与 `_create_streaming_message` 替换为：

```python
    async def generate(self, context: ModelContext) -> AssistantMessage:
        # 由 stream() 实现：同一个 provider 不得有两份解析逻辑
        return await accumulate(self.stream(context))

    async def stream(
        self, context: ModelContext
    ) -> AsyncIterator[StreamDelta]:
        # input_json_delta 事件不带 tool_call id，须由 content_block_start 记下
        tool_call_ids: dict[int, str] = {}

        async with self.client.messages.stream(
            **self._build_create_params(context)
        ) as stream:
            async for event in stream:
                event_type = getattr(event, "type", None)

                if event_type == "content_block_start":
                    block = getattr(event, "content_block", None)
                    if getattr(block, "type", None) == "tool_use":
                        index = getattr(event, "index", None)
                        block_id = getattr(block, "id", None)
                        if index is not None and block_id is not None:
                            tool_call_ids[index] = str(block_id)
                    continue

                if event_type != "content_block_delta":
                    continue

                delta = getattr(event, "delta", None)
                delta_type = getattr(delta, "type", None)

                if delta_type == "text_delta":
                    text = getattr(delta, "text", None)
                    if text:
                        yield TextDelta(text=str(text))
                elif delta_type == "thinking_delta":
                    thinking = getattr(delta, "thinking", None)
                    if thinking:
                        yield ThinkingDelta(text=str(thinking))
                elif delta_type == "input_json_delta":
                    partial = getattr(delta, "partial_json", None)
                    if partial:
                        yield ToolCallArgsDelta(
                            tool_call_id=tool_call_ids.get(
                                getattr(event, "index", -1), ""
                            ),
                            partial_json=str(partial),
                        )
                # signature_delta 不单独发：signature 由 SDK 累积进
                # get_final_message()，自行拼装反而会漏

            final = await stream.get_final_message()

        yield StreamCompleted(message=self._response_to_assistant_message(final))
```

import 段加：

```python
from typing import AsyncIterator

from pickel.providers.stream import (
    StreamCompleted,
    StreamDelta,
    TextDelta,
    ThinkingDelta,
    ToolCallArgsDelta,
    accumulate,
)
```

若 `AsyncIterator` 已在 typing import 中则不重复添加。

- [ ] **Step 4: 跑测试确认通过**

Run: `GEMINI_API_KEY=fake uv run --with pytest pytest tests/providers/ -q`
Expected: PASS（9 个新测试）

- [ ] **Step 5: 提交**

```bash
git checkout uv.lock
git add src/pickel/providers/anthropic.py tests/providers/test_anthropic_stream.py
git commit -m "feat(stream): anthropic 真流式，generate 由 stream 实现"
```

---

## Task 4: delta 与中断的 runtime 事件

**Files:**
- Modify: `src/pickel/runs/runtime_events.py`
- Test: `tests/runs/test_runtime_events.py`（追加）

**Interfaces:**
- Consumes: `EventEnvelope`、既有 `RuntimeEventBase`
- Produces: `ThinkingDeltaEvent(text)`；`TextDeltaEvent(text)`；`ToolCallArgsDeltaEvent(tool_call_id, partial_json)`；`TurnInterrupted(at_step, partial_text)`

**命名：** provider 层已有 `TextDelta` / `ThinkingDelta`（`providers/stream.py`），runtime 事件加 `Event` 后缀区分，与既有的 `AssistantMessageEvent` 一致。

**模块约束（`runtime_events.py` docstring 已写死）：** payload 里的可变对象必须是拷贝。本任务四个事件的 payload 全是 `str` / `int`，天然不可变，无需拷贝——但新增事件时这条仍然适用。

- [ ] **Step 1: 追加失败测试**

在 `tests/runs/test_runtime_events.py` 末尾追加：

```python
def test_delta_事件的_event_type_唯一且不与既有冲突():
    from pickel.runs.runtime_events import (
        TextDeltaEvent,
        ThinkingDeltaEvent,
        ToolCallArgsDeltaEvent,
        TurnInterrupted,
    )

    new_types = [
        ThinkingDeltaEvent, TextDeltaEvent, ToolCallArgsDeltaEvent, TurnInterrupted,
    ]
    old_types = [
        TurnStarted, StepStarted, ToolCallStarted, ToolCallCompleted,
        AssistantMessageEvent, TurnCompleted, TurnFailed,
    ]
    values = [cls.EVENT_TYPE for cls in new_types + old_types]

    assert len(set(values)) == len(values)


def test_delta_事件可_json_序列化():
    from pickel.runs.runtime_events import (
        TextDeltaEvent,
        ThinkingDeltaEvent,
        ToolCallArgsDeltaEvent,
        TurnInterrupted,
    )

    events = [
        ThinkingDeltaEvent(envelope=_envelope(), text="想"),
        TextDeltaEvent(envelope=_envelope(), text="你好"),
        ToolCallArgsDeltaEvent(
            envelope=_envelope(), tool_call_id="c1", partial_json='{"a"'
        ),
        TurnInterrupted(envelope=_envelope(), at_step=2, partial_text="写到一半"),
    ]

    for event in events:
        data = event.to_dict()
        json.dumps(data)
        assert data["seq"] == 3


def test_text_delta_事件载荷():
    from pickel.runs.runtime_events import TextDeltaEvent

    data = TextDeltaEvent(envelope=_envelope(), text="你好").to_dict()

    assert data["event_type"] == "text_delta"
    assert data["text"] == "你好"


def test_tool_call_args_delta_事件载荷():
    from pickel.runs.runtime_events import ToolCallArgsDeltaEvent

    data = ToolCallArgsDeltaEvent(
        envelope=_envelope(), tool_call_id="c1", partial_json='{"a": 1}'
    ).to_dict()

    assert data["event_type"] == "tool_call_args_delta"
    assert data["tool_call_id"] == "c1"
    assert data["partial_json"] == '{"a": 1}'


def test_turn_interrupted_载荷():
    from pickel.runs.runtime_events import TurnInterrupted

    data = TurnInterrupted(
        envelope=_envelope(), at_step=2, partial_text="写到一半"
    ).to_dict()

    assert data["event_type"] == "turn_interrupted"
    assert data["at_step"] == 2
    assert data["partial_text"] == "写到一半"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `GEMINI_API_KEY=fake uv run --with pytest pytest tests/runs/test_runtime_events.py -q`
Expected: FAIL — `ImportError: cannot import name 'TextDeltaEvent'`

- [ ] **Step 3: 实现**

在 `src/pickel/runs/runtime_events.py` 的 `TurnFailed` 之后追加：

```python
@dataclass(frozen=True)
class ThinkingDeltaEvent(RuntimeEventBase):
    EVENT_TYPE: ClassVar[str] = "thinking_delta"

    text: str = ""

    def _payload(self) -> dict[str, Any]:
        return {"text": self.text}


@dataclass(frozen=True)
class TextDeltaEvent(RuntimeEventBase):
    EVENT_TYPE: ClassVar[str] = "text_delta"

    text: str = ""

    def _payload(self) -> dict[str, Any]:
        return {"text": self.text}


@dataclass(frozen=True)
class ToolCallArgsDeltaEvent(RuntimeEventBase):
    """工具参数的增量 JSON；拼完才是合法 JSON，UI 不要中途解析。"""

    EVENT_TYPE: ClassVar[str] = "tool_call_args_delta"

    tool_call_id: str = ""
    partial_json: str = ""

    def _payload(self) -> dict[str, Any]:
        return {
            "tool_call_id": self.tool_call_id,
            "partial_json": self.partial_json,
        }


@dataclass(frozen=True)
class TurnInterrupted(RuntimeEventBase):
    """用户中断；partial_text 是已生成但未完成的正文。"""

    EVENT_TYPE: ClassVar[str] = "turn_interrupted"

    at_step: int = 0
    partial_text: str = ""

    def _payload(self) -> dict[str, Any]:
        return {"at_step": self.at_step, "partial_text": self.partial_text}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `GEMINI_API_KEY=fake uv run --with pytest pytest tests/runs/test_runtime_events.py -q`
Expected: PASS

- [ ] **Step 5: 更新 `runs/__init__.py` 导出**

在既有的 `from pickel.runs.runtime_events import (...)` 里加入四个新名字，并同步加进 `__all__`（保持字母序）。

- [ ] **Step 6: 跑全仓确认无回归**

Run: `GEMINI_API_KEY=fake uv run --with pytest pytest -q`
Expected: 只剩基线的 6 个 `test_shell.py` 失败

- [ ] **Step 7: 提交**

```bash
git checkout uv.lock
git add -A
git commit -m "feat(events): delta 与 turn_interrupted 事件类型"
```

---

## Task 5: react 消费 stream 并发 delta 事件

**Files:**
- Modify: `src/pickel/runs/strategy/react.py:129-132`（调用点）与 `:313-320`（`_generate_with_optional_timeout`）
- Test: `tests/runs/test_react_streaming.py`

**Interfaces:**
- Consumes: `provider.stream()`（Task 2/3）、delta 事件（Task 4）、`EventBus`
- Produces: `ReActStrategy._generate_streaming(run, context, bus, envelope) -> AssistantMessage`——消费 stream、发 delta 事件、返回最终消息

**超时语义保持不变：** 现在是 `asyncio.wait_for(provider.generate(ctx), timeout)`，即**整个请求**的总时长上限（默认 600s，见 `DEFAULT_PROVIDER_TIMEOUT_SECONDS`）。改流式后仍用总时长包住整个消费循环，不引入 idle timeout——后者是新语义，本轮不做（YAGNI）。

**tool_snapshot 不得重取：** `turn.tool_snapshot` 在 `react.py:69` 于 turn 开头取一次。streaming 循环在 step 内部，绝不能碰它。

- [ ] **Step 1: 写失败测试**

```python
# tests/runs/test_react_streaming.py
"""react 消费 provider.stream 并把增量转成 runtime 事件。"""

from __future__ import annotations

import asyncio
import unittest
from pathlib import Path
from typing import AsyncIterator

from pickel.agents.agent import Agent
from pickel.context.assembler import ContextAssembler
from pickel.context.model_context import ModelContext
from pickel.conversations.agent_message import (
    AssistantMessage,
    ModelResponseMetadata,
    ModelUsage,
)
from pickel.conversations.content_blocks import TextContent
from pickel.conversations.session import Session
from pickel.hooks.lifecycle import NoopLifecycleHooks
from pickel.providers.stream import (
    StreamCompleted,
    StreamDelta,
    TextDelta,
    ThinkingDelta,
)
from pickel.runs import ReActStrategy, Run
from pickel.runs.event_bus import EventBus
from pickel.runs.runtime_events import (
    AssistantMessageEvent,
    TextDeltaEvent,
    ThinkingDeltaEvent,
)
from pickel.shared.model_config import ModelConfig
from pickel.tools.bus import ToolActivation, bus_with
from pickel.tools.shell import ShellSessionManager


def _reply() -> AssistantMessage:
    return AssistantMessage(
        content=[TextContent(text="你好")],
        metadata=ModelResponseMetadata(
            provider="fake",
            model="fake-1",
            usage=ModelUsage(input_tokens=100, output_tokens=10),
        ),
    )


class _StreamingProvider:
    """产出增量的 provider；generate 由 stream 实现。"""

    def __init__(self) -> None:
        self.stream_calls = 0

    async def stream(self, context: ModelContext) -> AsyncIterator[StreamDelta]:
        self.stream_calls += 1
        yield ThinkingDelta(text="想")
        yield TextDelta(text="你")
        yield TextDelta(text="好")
        yield StreamCompleted(message=_reply())

    async def generate(self, context: ModelContext) -> AssistantMessage:
        from pickel.providers.stream import accumulate

        return await accumulate(self.stream(context))


def _run(provider) -> Run:
    bus_obj = bus_with([])
    return Run(
        agent=Agent(
            agent_id="Pickle",
            workspace_path=Path("."),
            behavior_path=Path("."),
            behavior_instruction="you are pickle",
            model_config=ModelConfig(provider="fake", model="fake-1"),
            tool_ids=[],
        ),
        provider=provider,
        tool_bus=bus_obj,
        activation=ToolActivation(allowed=frozenset(bus_obj.list_names())),
        context_assembler=ContextAssembler(),
        lifecycle_hooks=NoopLifecycleHooks(),
        session_service=None,
        file_access_policy=None,
        workspace_files=None,
        shell_session_manager=ShellSessionManager(),
        unit_window=5,
        strategy=ReActStrategy(max_steps=2),
    )


class ReactStreamingTests(unittest.IsolatedAsyncioTestCase):
    async def _collect(self, provider):
        session = Session.create(agent_id="Pickle", session_id="s1")
        bus = EventBus()
        events = []
        bus.subscribe(lambda e: events.append(e))
        await _run(provider).turn(session=session, user_text="hi", bus=bus)
        return events

    async def test_文本增量被转成_text_delta_事件(self) -> None:
        events = await self._collect(_StreamingProvider())
        texts = [e.text for e in events if isinstance(e, TextDeltaEvent)]

        self.assertEqual(["你", "好"], texts)

    async def test_思考增量被转成_thinking_delta_事件(self) -> None:
        events = await self._collect(_StreamingProvider())
        thinking = [e.text for e in events if isinstance(e, ThinkingDeltaEvent)]

        self.assertEqual(["想"], thinking)

    async def test_delta_事件在_assistant_message_之前(self) -> None:
        """增量必须先到，否则 UI 无法边生成边显示。"""
        events = await self._collect(_StreamingProvider())
        kinds = [type(e).__name__ for e in events]
        last_delta = max(
            i for i, k in enumerate(kinds) if k.endswith("DeltaEvent")
        )
        assistant = kinds.index("AssistantMessageEvent")

        self.assertLess(last_delta, assistant)

    async def test_delta_事件带完整信封(self) -> None:
        events = await self._collect(_StreamingProvider())
        deltas = [e for e in events if isinstance(e, TextDeltaEvent)]

        for event in deltas:
            self.assertEqual("s1", event.envelope.session_id)
            self.assertTrue(event.envelope.turn_id)
            self.assertEqual(1, event.envelope.step_index)

    async def test_seq_在_delta_之间仍然连续(self) -> None:
        events = await self._collect(_StreamingProvider())

        self.assertEqual(
            list(range(len(events))), [e.envelope.seq for e in events]
        )

    async def test_只调用一次_stream(self) -> None:
        provider = _StreamingProvider()
        await self._collect(provider)

        self.assertEqual(1, provider.stream_calls)

    async def test_非流式_provider_不产生_delta_事件(self) -> None:
        """只实现 generate 的 provider 走基类默认 stream，行为不变。"""

        class _Plain:
            async def generate(self, context):
                return _reply()

            async def stream(self, context):
                yield StreamCompleted(message=await self.generate(context))

        events = await self._collect(_Plain())
        deltas = [e for e in events if type(e).__name__.endswith("DeltaEvent")]

        self.assertEqual([], deltas)
        self.assertTrue(
            any(isinstance(e, AssistantMessageEvent) for e in events)
        )


if __name__ == "__main__":
    unittest.main()
```

**`Run` 的构造已按当前签名写好**（T1 之后是 `@dataclass`，字段含 `tool_bus` 与 `activation`，已无 `tools`）。`bus_with([])` 与 `ToolActivation(allowed=frozenset(...))` 的用法与 `tests/runs/test_events.py:150-164` 的既有 `_run()` 一致。

- [ ] **Step 2: 跑测试确认失败**

Run: `GEMINI_API_KEY=fake uv run --with pytest pytest tests/runs/test_react_streaming.py -q`
Expected: FAIL——当前 react 调 `generate()`，不产生任何 delta 事件

- [ ] **Step 3: 实现**

`react.py:129-132` 的调用点改为：

```python
            assistant = await self._generate_streaming(
                run=run,
                context=model_context,
                bus=bus,
                envelope=envelope,
                step_index=step_index,
            )
```

`_generate_with_optional_timeout`（`:313-320`）替换为：

```python
    async def _generate_streaming(
        self,
        *,
        run: Run,
        context,
        bus: EventBus | None,
        envelope,
        step_index: int,
    ) -> AssistantMessage:
        """消费 provider.stream，把增量转成事件，返回最终消息。

        超时语义与改造前一致：包住整个消费过程的总时长，
        不是两次 delta 之间的间隔。
        """
        timeout_seconds = self._provider_timeout_seconds(run)
        coro = self._consume_stream(
            run=run,
            context=context,
            bus=bus,
            envelope=envelope,
            step_index=step_index,
        )
        if timeout_seconds is None:
            return await coro
        return await asyncio.wait_for(coro, timeout=timeout_seconds)

    async def _consume_stream(
        self,
        *,
        run: Run,
        context,
        bus: EventBus | None,
        envelope,
        step_index: int,
    ) -> AssistantMessage:
        message: AssistantMessage | None = None
        async for delta in run.provider.stream(context):
            if isinstance(delta, StreamCompleted):
                message = delta.message
                break
            event = self._delta_to_event(delta, envelope(step_index))
            if event is not None:
                await self._emit(bus, event)
        if message is None:
            raise ValueError("provider.stream 未以 StreamCompleted 收尾")
        return message

    @staticmethod
    def _delta_to_event(delta, envelope_value):
        if isinstance(delta, TextDelta):
            return TextDeltaEvent(envelope=envelope_value, text=delta.text)
        if isinstance(delta, ThinkingDelta):
            return ThinkingDeltaEvent(envelope=envelope_value, text=delta.text)
        if isinstance(delta, ToolCallArgsDelta):
            return ToolCallArgsDeltaEvent(
                envelope=envelope_value,
                tool_call_id=delta.tool_call_id,
                partial_json=delta.partial_json,
            )
        return None
```

`_provider_timeout_seconds` 保持不动。import 段加：

```python
from pickel.providers.stream import (
    StreamCompleted,
    TextDelta,
    ThinkingDelta,
    ToolCallArgsDelta,
)
from pickel.runs.runtime_events import (
    TextDeltaEvent,
    ThinkingDeltaEvent,
    ToolCallArgsDeltaEvent,
)
```

（`runtime_events` 的 import 已存在，追加这三个名字即可。）

- [ ] **Step 4: 跑测试确认通过**

Run: `GEMINI_API_KEY=fake uv run --with pytest pytest tests/runs/ -q`
Expected: PASS

- [ ] **Step 5: 确认没有重取工具快照**

```bash
grep -n "tool_bus.snapshot" src/pickel/runs/strategy/react.py
```

Expected: 只有一处，在 `execute` 开头（约 :69）。若 streaming 相关代码里出现第二处，删掉——每 step 重取会让 prompt cache 失效。

- [ ] **Step 6: 提交**

```bash
git checkout uv.lock
git add -A
git commit -m "feat(stream): react 消费 provider.stream 并发 delta 事件"
```

---

## Task 6: 中断语义

**Files:**
- Modify: `src/pickel/runs/strategy/react.py`（工具循环的取消处理）
- Modify: `src/pickel/runs/run.py`（`TurnInterrupted` 事件）
- Test: `tests/runs/test_interrupt.py`

**Interfaces:**
- Consumes: `TurnInterrupted`（Task 4）
- Produces: 中断后 session 处于可继续状态——active_path 上不存在缺 `tool_result` 的 `tool_call`

**这是正确性问题，不是体验问题。** session 里若留下一条没有对应 `tool_result` 的 `tool_call`，下一轮请求会被 provider 直接拒绝（Anthropic 与 Gemini 都要求 tool_use 与 tool_result 配对）。中断不能把会话弄成不可继续的状态。

**取消的传播路径：** UI 捕 Ctrl-C → `asyncio.Task.cancel()` → `CancelledError` 在 `await` 点抛出 → react 捕获 → 补齐落盘 → 重新抛出。

**必须重新抛出：** `CancelledError` 在 Python 3.8+ 继承自 `BaseException` 而非 `Exception`。吞掉它会让 asyncio 的取消机制失效。捕获只为落盘补齐，补完必须 `raise`。

- [ ] **Step 1: 写失败测试**

```python
# tests/runs/test_interrupt.py
"""中断语义：session 必须保持可继续。"""

from __future__ import annotations

import asyncio
import unittest
from pathlib import Path

from pickel.agents.agent import Agent
from pickel.context.assembler import ContextAssembler
from pickel.conversations.agent_message import (
    AssistantMessage,
    ModelResponseMetadata,
    ModelUsage,
    ToolResultMessage,
    agent_message_from_dict,
)
from pickel.conversations.content_blocks import TextContent, ToolCallContent
from pickel.conversations.session import Session
from pickel.conversations.session_entry import ENTRY_TYPE_MESSAGE
from pickel.hooks.lifecycle import NoopLifecycleHooks
from pickel.runs import ReActStrategy, Run
from pickel.runs.event_bus import EventBus
from pickel.runs.runtime_events import TurnInterrupted
from pickel.shared.model_config import ModelConfig
from pickel.tools.base import BaseTool, ToolExecutionContext, ToolExecutionResult, ToolSpec
from pickel.tools.bus import ToolActivation, bus_with
from pickel.tools.shell import ShellSessionManager


class _HangingTool(BaseTool):
    """永远不返回的工具——用来模拟「中断时工具正在跑」。"""

    spec = ToolSpec(
        name="hang",
        description="Hangs forever",
        input_schema={"type": "object", "properties": {}},
    )

    async def execute(self, arguments, context) -> ToolExecutionResult:
        await asyncio.sleep(3600)
        return ToolExecutionResult(content="never")


class _ToolCallProvider:
    async def generate(self, context):
        return AssistantMessage(
            content=[ToolCallContent(id="call_1", name="hang", arguments={})],
            metadata=ModelResponseMetadata(
                provider="fake",
                model="fake-1",
                usage=ModelUsage(input_tokens=100, output_tokens=10),
            ),
        )

    async def stream(self, context):
        from pickel.providers.stream import StreamCompleted

        yield StreamCompleted(message=await self.generate(context))


def _messages(session):
    out = []
    for entry in session.active_path():
        if entry.entry_type != ENTRY_TYPE_MESSAGE:
            continue
        try:
            out.append(agent_message_from_dict(entry.payload))
        except (KeyError, TypeError, ValueError):
            continue
    return out


class InterruptTests(unittest.IsolatedAsyncioTestCase):
    def _run(self, provider, tools):
        # bus_with 按 BUILTIN 来源注册，内置工具用裸名，故 tool_ids=["hang"] 能匹配
        bus_obj = bus_with(tools)
        return Run(
            agent=Agent(
                agent_id="Pickle",
                workspace_path=Path("."),
                behavior_path=Path("."),
                behavior_instruction="you are pickle",
                model_config=ModelConfig(provider="fake", model="fake-1"),
                tool_ids=["hang"],
            ),
            provider=provider,
            tool_bus=bus_obj,
            activation=ToolActivation(allowed=frozenset(bus_obj.list_names())),
            context_assembler=ContextAssembler(),
            lifecycle_hooks=NoopLifecycleHooks(),
            session_service=None,
            file_access_policy=None,
            workspace_files=None,
            shell_session_manager=ShellSessionManager(),
            unit_window=5,
            strategy=ReActStrategy(max_steps=2),
        )

    async def test_中断后不存在缺_tool_result_的_tool_call(self) -> None:
        """悬空 tool_call 会让下一轮请求被 provider 拒绝。"""
        session = Session.create(agent_id="Pickle", session_id="s1")
        run = self._run(_ToolCallProvider(), [_HangingTool()])
        bus = EventBus()

        task = asyncio.create_task(
            run.turn(session=session, user_text="hi", bus=bus)
        )
        await asyncio.sleep(0.05)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        messages = _messages(session)
        call_ids = {
            block.id
            for message in messages
            if isinstance(message, AssistantMessage)
            for block in message.content
            if isinstance(block, ToolCallContent)
        }
        result_ids = {
            message.tool_call_id
            for message in messages
            if isinstance(message, ToolResultMessage)
        }

        self.assertTrue(call_ids, "测试前提：应该已经落盘了一条 tool_call")
        self.assertEqual(call_ids, result_ids)

    async def test_中断补齐的_tool_result_标记为错误(self) -> None:
        session = Session.create(agent_id="Pickle", session_id="s1")
        run = self._run(_ToolCallProvider(), [_HangingTool()])

        task = asyncio.create_task(
            run.turn(session=session, user_text="hi", bus=EventBus())
        )
        await asyncio.sleep(0.05)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        results = [
            m for m in _messages(session) if isinstance(m, ToolResultMessage)
        ]
        self.assertEqual(1, len(results))
        self.assertTrue(results[0].is_error)

    async def test_中断发出_turn_interrupted_事件(self) -> None:
        session = Session.create(agent_id="Pickle", session_id="s1")
        run = self._run(_ToolCallProvider(), [_HangingTool()])
        bus = EventBus()
        events = []
        bus.subscribe(lambda e: events.append(e))

        task = asyncio.create_task(
            run.turn(session=session, user_text="hi", bus=bus)
        )
        await asyncio.sleep(0.05)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        interrupted = [e for e in events if isinstance(e, TurnInterrupted)]
        self.assertEqual(1, len(interrupted))
        self.assertEqual(1, interrupted[0].at_step)

    async def test_中断不发_turn_completed_也不发_turn_failed(self) -> None:
        from pickel.runs.runtime_events import TurnCompleted, TurnFailed

        session = Session.create(agent_id="Pickle", session_id="s1")
        run = self._run(_ToolCallProvider(), [_HangingTool()])
        bus = EventBus()
        events = []
        bus.subscribe(lambda e: events.append(e))

        task = asyncio.create_task(
            run.turn(session=session, user_text="hi", bus=bus)
        )
        await asyncio.sleep(0.05)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        self.assertFalse([e for e in events if isinstance(e, TurnCompleted)])
        self.assertFalse([e for e in events if isinstance(e, TurnFailed)])

    async def test_CancelledError_必须重新抛出(self) -> None:
        """吞掉它会让 asyncio 的取消机制失效。"""
        session = Session.create(agent_id="Pickle", session_id="s1")
        run = self._run(_ToolCallProvider(), [_HangingTool()])

        task = asyncio.create_task(
            run.turn(session=session, user_text="hi", bus=EventBus())
        )
        await asyncio.sleep(0.05)
        task.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(task.cancelled() or task.done())


if __name__ == "__main__":
    unittest.main()
```

**`bus_with(tools)` 按 `ToolSource.BUILTIN` 注册，内置工具用裸名**（`qualified_name` 对 BUILTIN 直接返回 `spec_name`），所以 `agent.tool_ids=["hang"]` 能匹配到 `_HangingTool`。不要手写 `ToolBus().register(...)`——它的 `source` 是必需关键字参数且返回的是限定名。

- [ ] **Step 2: 跑测试确认失败**

Run: `GEMINI_API_KEY=fake uv run --with pytest pytest tests/runs/test_interrupt.py -q`
Expected: FAIL——当前中断后 session 里留下悬空 tool_call

- [ ] **Step 3: 实现补齐落盘**

在 `react.py` 的工具执行循环外层包一个取消处理。找到 `for call_index, tool_call in enumerate(tool_calls):` 这个循环，把它整体包进 `try`：

```python
            try:
                for call_index, tool_call in enumerate(tool_calls):
                    # ... 既有循环体保持不变 ...
            except asyncio.CancelledError:
                # 中断时补齐未完成的 tool_result：session 里留下悬空的
                # tool_call 会让下一轮请求被 provider 直接拒绝。
                self._complete_pending_tool_calls(
                    run=run,
                    session=session,
                    step=step,
                    tool_calls=tool_calls,
                )
                await self._emit(
                    bus,
                    TurnInterrupted(
                        envelope=envelope(step_index),
                        at_step=step_index,
                        partial_text=self._assistant_text(assistant),
                    ),
                )
                raise
```

新增方法：

```python
    def _complete_pending_tool_calls(
        self,
        *,
        run: Run,
        session: Session,
        step,
        tool_calls: list[ToolCallContent],
    ) -> None:
        """给已落盘但未完成的 tool_call 补一条中断标记的 tool_result。"""
        completed = set(step.completed_tool_call_ids)
        for tool_call in tool_calls:
            if tool_call.id in completed:
                continue
            entry = session.append_tool_result(
                ToolResultMessage(
                    tool_call_id=tool_call.id,
                    tool_name=tool_call.name,
                    content=[TextContent(text="工具执行被用户中断")],
                    is_error=True,
                )
            )
            self._flush_entry(run, session, entry)
```

import 段加 `import asyncio`（若已有则跳过）与 `from pickel.runs.runtime_events import TurnInterrupted`（追加到既有 import）。

- [ ] **Step 4: 让 run.turn 不把取消当失败**

`run.py` 的 `try/except Exception` 之外，`CancelledError` 会自然穿透（它继承 `BaseException`），所以 `TurnFailed` 不会被发出——这是正确行为，无需改动。但要确认 `TurnCompleted` 也不会被发出：`await emit(TurnCompleted(...))` 在 `try` 之后，取消穿透时不会执行到。

跑测试确认这一点已经成立；若发现 `TurnCompleted` 仍被发出，检查 `run.py` 是否有 `except BaseException` 或 `finally` 块误发。

- [ ] **Step 5: 跑测试确认通过**

Run: `GEMINI_API_KEY=fake uv run --with pytest pytest tests/runs/ -q`
Expected: PASS

- [ ] **Step 6: 提交**

```bash
git checkout uv.lock
git add -A
git commit -m "feat(interrupt): 中断补齐悬空 tool_result，发 turn_interrupted"
```

---

## Task 7: 流式渲染

**Files:**
- Modify: `src/pickel/cli/event_renderer.py`
- Modify: `src/pickel/cli/chat.py`（仅渲染订阅段与中断捕获）
- Test: `tests/cli/test_streaming_render.py`

**Interfaces:**
- Consumes: delta 事件（Task 4）、`TurnInterrupted`
- Produces: `ChatEventRenderer` 消费 delta 并增量输出；`ChatLoop` 捕获 Ctrl-C 取消当前 turn 而非退出

**范围限制：** 本任务只让文字增量出现，**不做无边框排版**——那是 E3。Panel 布局保持不变，`AssistantMessageEvent` 到达时仍渲染完整的 Assistant 框（含 footer）。流式输出是它之前的「预览」。

**为什么不用 rich `Live`：** `Live` 会接管屏幕区域，与 prompt-toolkit 的输入共存有真实难度（设计稿 §12 已把「底部固定状态栏」列为不做）。本任务用最简形式：delta 直接 `console.print(..., end="")` 增量写出，`AssistantMessageEvent` 到达前先换行。这样零屏幕接管、零与输入冲突。

- [ ] **Step 1: 写失败测试**

```python
# tests/cli/test_streaming_render.py
"""流式渲染：delta 增量出字，最终仍渲染完整 Assistant 框。"""

from __future__ import annotations

import asyncio

from rich.console import Console

from pickel.cli.event_renderer import ChatEventRenderer
from pickel.runs.runtime_events import (
    AssistantMessageEvent,
    TextDeltaEvent,
    ThinkingDeltaEvent,
    ToolCallArgsDeltaEvent,
    TurnInterrupted,
)
from pickel.runs.turn_usage import TurnUsage


def _render(events) -> str:
    console = Console(width=100, record=True, force_terminal=False)
    renderer = ChatEventRenderer(console)
    for event in events:
        asyncio.run(renderer.handle_event(event))
    return console.export_text()


def test_文本增量按顺序出现():
    text = _render(
        [TextDeltaEvent(text="你"), TextDeltaEvent(text="好"), TextDeltaEvent(text="呀")]
    )

    assert "你好呀" in text


def test_增量之后仍渲染完整_assistant_框():
    text = _render(
        [
            TextDeltaEvent(text="你好"),
            AssistantMessageEvent(
                text="你好",
                usage=TurnUsage(
                    steps=1, input_tokens=100, output_tokens=20,
                    elapsed_ms=1500, model_label="anthropic / claude-jupiter-v1-p",
                ),
            ),
        ]
    )

    assert "anthropic / claude-jupiter-v1-p" in text
    assert "in 100 · out 20 · 1.5s" in text


def test_思考增量与正文增量都出现():
    text = _render([ThinkingDeltaEvent(text="想一下"), TextDeltaEvent(text="答案")])

    assert "想一下" in text
    assert "答案" in text


def test_工具参数增量不上屏():
    """partial_json 拼完前不是合法 JSON，展示半截参数只会制造噪音。"""
    text = _render(
        [ToolCallArgsDeltaEvent(tool_call_id="c1", partial_json='{"text"')]
    )

    assert '{"text"' not in text


def test_中断显示提示():
    text = _render([TurnInterrupted(at_step=2, partial_text="写到一半")])

    assert "中断" in text


def test_无_delta_时渲染与改造前一致():
    """非流式 provider 走这条路径，输出不得变化。"""
    text = _render(
        [
            AssistantMessageEvent(
                text="完整回复",
                usage=TurnUsage(
                    steps=1, input_tokens=100, output_tokens=20,
                    elapsed_ms=1500, model_label="anthropic / claude-jupiter-v1-p",
                ),
            )
        ]
    )

    assert "完整回复" in text
    assert "in 100 · out 20 · 1.5s" in text
```

- [ ] **Step 2: 跑测试确认失败**

Run: `GEMINI_API_KEY=fake uv run --with pytest pytest tests/cli/test_streaming_render.py -q`
Expected: FAIL——renderer 尚未处理 delta 事件

- [ ] **Step 3: 实现 renderer**

在 `ChatEventRenderer.handle_event` 的既有分派链前面加 delta 分支：

```python
        if isinstance(event, (TextDeltaEvent, ThinkingDeltaEvent)):
            self._streaming = True
            self.console.print(event.text, end="", highlight=False, markup=False)
            return

        if isinstance(event, ToolCallArgsDeltaEvent):
            # partial_json 拼完前不是合法 JSON，展示半截只会制造噪音
            return

        if isinstance(event, TurnInterrupted):
            self._end_streaming()
            self._render_message(
                "System", Text("已中断本轮。"), style="yellow"
            )
            return
```

在 `AssistantMessageEvent` 分支的开头加 `self._end_streaming()`，在 `StepStarted` 与 `ToolCallStarted` 分支开头也加——任何要画框的事件到来前，先把流式输出收尾。

新增：

```python
    def _end_streaming(self) -> None:
        """流式输出与框式输出之间补一个换行。"""
        if self._streaming:
            self.console.print()
            self._streaming = False
```

`__init__` 加 `self._streaming = False`。import 段加四个新事件类型。

- [ ] **Step 4: 实现 chat.py 的中断捕获**

`_loop()` 里包住 turn 的 `try` 加一个分支。当前结构是 `try: reply = await self.handle_user_input(...) except Exception:`，改为把 turn 放进 task 并捕获 `KeyboardInterrupt`：

```python
            bus, event_renderer, unsubscribe_renderer = self.create_event_bus()
            start_index = len(self.session.entries)
            task = asyncio.create_task(self.handle_user_input(user_input, bus=bus))
            try:
                reply = await task
                if self._session_service is not None:
                    self._session_service.flush_new_entries(
                        session=self.session,
                        entries=[],
                    )
            except KeyboardInterrupt:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, KeyboardInterrupt):
                    pass
                # 中断时 react 已补齐 tool_result 并落盘，这里再 flush 一次
                if self._session_service is not None:
                    self._session_service.flush_new_entries(
                        session=self.session,
                        entries=[],
                    )
                continue
            except asyncio.CancelledError:
                continue
            except Exception:
                self._render_error_message(traceback.format_exc().rstrip())
                continue
            finally:
                unsubscribe_renderer()
```

**`flush_new_entries` 那两段不能漏**——改造前它在 `try` 里（`chat.py:638-642`），漏掉会静默丢失 session 落盘。中断分支也要 flush：react 在取消时补齐了 tool_result，那些 entry 需要落盘，否则下一轮从磁盘恢复的 session 仍有悬空 tool_call。

顺带把 `except Exception as exc` 改成 `except Exception:`——`exc` 是未使用变量（ruff F841）。

`import asyncio` 加到 `chat.py` 顶部（若无）。

**这一段只碰渲染订阅与中断，不碰装配与 `/reload`**——那是工具总线线程的区域。

- [ ] **Step 5: 跑测试确认通过**

Run: `GEMINI_API_KEY=fake uv run --with pytest pytest -q`
Expected: 只剩基线的 6 个 `test_shell.py` 失败

- [ ] **Step 6: 提交**

```bash
git checkout uv.lock
git add -A
git commit -m "feat(cli): delta 增量出字，Ctrl-C 中断当前 turn"
```

---

## Task 8: 端到端与真实模型验证

**Files:**
- Test: scratchpad 脚本，不进仓库

**Interfaces:**
- Consumes: 全部前序任务

- [ ] **Step 1: 组装层集成测试**

在 `tests/cli/test_chat_loop.py` 追加一条：真 `Run` + 流式 fake provider + trace 开启，断言 trace 里 delta 事件与 `assistant_message` 的顺序、seq 连续。参照该文件里已有的 `test_真实_run_端到端_事件序列_seq_trace_与渲染次数` 的构造方式，把 provider 换成产出增量的版本。

- [ ] **Step 2: 跑全仓**

Run: `GEMINI_API_KEY=fake uv run --with pytest pytest -q`
Expected: 只剩基线的 6 个 `test_shell.py` 失败

- [ ] **Step 3: 真实模型验证**

写到 scratchpad（不进仓库），用 `claude-jupiter-v1-p` 跑一轮带工具的对话。加载环境变量：

```bash
set -a && . ~/.pickel/.env && set +a && PICKEL_TRACE=1 uv run python <scratchpad>/e2_streaming.py
```

**绝对不要打印任何环境变量的值**，不要 `cat ~/.pickel/.env`。需要确认变量存在时只用 `cut -d= -f1` 列变量名。

脚本要验证 fake provider 覆盖不到的三件事：

1. 真实 SSE 事件下 `text_delta` 事件的数量远大于 1（证明确实在流式，而非一次性给出）
2. `thinking` 块的 signature 在最终消息里完整保留（把它作为下一轮输入再发一次，确认不被 provider 拒绝）
3. trace 里 delta 事件与 `assistant_message` 的 seq 连续无缺口

脚本骨架：

```python
import asyncio, json, os
from pathlib import Path

os.environ["PICKEL_TRACE"] = "1"

from pickel.app.boot import Boot
from pickel.config.loader import Config
from pickel.conversations.session import Session
from pickel.runs.event_bus import EventBus
from pickel.runs.runtime_events import (
    AssistantMessageEvent, TextDeltaEvent, ThinkingDeltaEvent,
)
from pickel.runs.trace_sink import JsonlTraceSink, trace_path


async def main() -> None:
    boot = Boot.from_config(Config.load(cwd=Path.cwd()))
    agent, run = boot.build_run()
    session = Session.create(agent_id=agent.agent_id)

    path = trace_path(session.session_id)
    if path.exists():
        path.unlink()
    sink = JsonlTraceSink(path)
    bus = EventBus()
    events = []
    bus.subscribe(lambda e: events.append(e))
    bus.subscribe(sink)

    await run.turn(
        session=session,
        user_text="用 shell_exec 跑 `echo hello-e2`，然后用两三句话说明输出。",
        bus=bus,
    )
    sink.close()

    text_deltas = [e for e in events if isinstance(e, TextDeltaEvent)]
    thinking_deltas = [e for e in events if isinstance(e, ThinkingDeltaEvent)]
    print(f"text_delta 数量: {len(text_deltas)}")
    print(f"thinking_delta 数量: {len(thinking_deltas)}")
    print(f"拼接文本: {''.join(e.text for e in text_deltas)[:200]}")

    assert len(text_deltas) > 1, "只有 1 个 text_delta，说明没有真流式"

    assert [e.envelope.seq for e in events] == list(range(len(events)))

    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == len(events)
    for line in lines:
        json.loads(line)

    # 第二轮：把带 signature 的历史再发一次，确认不被拒绝
    await run.turn(session=session, user_text="再说一句。", bus=bus)
    print("第二轮通过——thinking signature 未丢失")

    print("全部通过")


asyncio.run(main())
```

若模型不调工具，调整提示词重试，**最多 2 次**（真实付费调用）。

- [ ] **Step 4: 交互确认中断**

```bash
set -a && . ~/.pickel/.env && set +a && uv run pickel chat
```

问一个会产生长回复的问题，在生成过程中按 Ctrl-C。确认：

- 输出停下，显示「已中断本轮」
- **没有退出 chat**，仍能继续输入
- 紧接着再问一个问题，能正常回复（证明 session 未被中断破坏）

退出后确认 `~/.pickel/traces/` 无新文件（trace 默认关）。

- [ ] **Step 5: 提交**

```bash
git checkout uv.lock
git add -A
git commit -m "test(stream): 组装层集成测试，E2 完成"
```

---

## 验收清单

- [ ] 全仓测试：基线 6 个 shell 失败不增不减，新增测试全绿
- [ ] `grep -rn "rich" src/pickel/runs/ src/pickel/providers/` 无输出
- [ ] `grep -n "tool_bus.snapshot" src/pickel/runs/strategy/react.py` 只有一处（turn 开头）
- [ ] anthropic 的 `generate()` 与 `accumulate(stream())` 逐字段相等（契约测试）
- [ ] 真实模型下 `text_delta` 数量 > 1
- [ ] thinking signature 在第二轮回传中未被拒绝
- [ ] 中断后 session 无悬空 tool_call，且能继续对话
- [ ] 中断不退出 chat
- [ ] trace 默认关：不设 `PICKEL_TRACE` 跑一轮，`~/.pickel/traces/` 无新文件

## 给 E3 的接口约定

E3 重做 UI 时，delta 事件已经就位。E2 的渲染是最小形态（直接增量 print，无屏幕接管），E3 可以换成任何形式而不必改 runtime。

E2 遗留、E3 需处理：

- footer 在 `usage=None` 时整体消失（旧代码显示 model 行）
- `chat.py` 的 `_render_assistant_message` 仍自行拼装 `MessageMetadata`（fallback 路径）
- 无边框排版、工具行原地 `running → ok`
- `ToolCallArgsDeltaEvent` 目前不上屏；E3 若要做「参数边生成边显示」，需要在 UI 侧做增量 JSON 的容错解析
