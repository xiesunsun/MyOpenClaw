# E1 事件底座 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 runtime 事件从「无信封的扁平 dataclass + 单 handler」换成「带全序信封的 tagged union + 多订阅 EventBus + 可选 JSONL trace」，UI 行为保持不变。

**Architecture:** 信封放 `shared/`（hooks 与 runs 都要用，且 hooks 不能反向依赖 runs）。事件类型按时机拆成独立 dataclass，各自带且仅带自己的字段。EventBus 统一分配 `seq` 并隔离订阅者异常。E1 结束时 UI 输出与今天逐字节一致——这是纯底座替换，delta 与新排版留给 E2/E3。

**Tech Stack:** Python 3.12、uv、pytest、dataclasses、asyncio

## Global Constraints

- runtime 不得 import `rich` 或任何 UI 库
- 订阅者异常不得影响 turn 执行
- `seq` 只由 EventBus 分配，发射点不得自行编号
- trace 是派生物，禁止任何代码从中读回重建对话或用量
- trace 默认关闭
- hook 事件与 runtime 事件共享信封字段，但不共享语义——runtime 订阅者不得具备改写能力
- TDD：每个任务先写红灯测试，确认失败后再实现
- 测试命令统一带 `GEMINI_API_KEY=fake`（缺失会导致 12 个无关测试失败）
- 运行测试用 `uv run --with pytest pytest`
- 每个任务结束前跑一次 `git checkout uv.lock`（测试运行会修改它）

**基线：** 改动前 `343 passed, 1 skipped, 6 failed`。那 6 个是 `tests/tools/test_shell.py` 的 bash bracketed-paste 环境问题，与本计划无关，全程应保持 6 个不增不减。

---

## 文件地图

| 文件 | 责任 | 动作 |
|---|---|---|
| `src/pickel/shared/event_envelope.py` | `EventIdentity` / `EventEnvelope` | 新建 |
| `src/pickel/runs/runtime_events.py` | tagged union 事件类型 + `to_dict` | 新建 |
| `src/pickel/runs/event_bus.py` | 订阅、`seq` 分配、异常隔离 | 新建 |
| `src/pickel/runs/trace_sink.py` | JSONL 落盘订阅者 | 新建 |
| `src/pickel/runs/events.py` | 旧扁平事件 | Task 8 删除 |
| `src/pickel/hooks/events.py` | hook 事件 | 改基类 |
| `src/pickel/runs/strategy/react.py` | 发射点 | 改写 + 删死代码 |
| `src/pickel/runs/run.py` | turn 边界 | 加 turn 级事件 |
| `src/pickel/runs/strategy/base.py` | `RuntimeEventHandler` 重复定义 | 删重复 |
| `src/pickel/runs/__init__.py` | 导出 | 改 |
| `src/pickel/config/loader.py` | `_BUILTIN_DEFAULTS` 加 `trace_enabled` | 改 |
| `src/pickel/config/app_config.py` | `AppConfig.trace_enabled` 字段 | 改 |
| `src/pickel/cli/event_renderer.py` | 渲染 | 适配新事件 |
| `src/pickel/cli/chat.py` | 订阅 | 改 `create_event_handler` |

---

## Task 1: 事件信封

**Files:**
- Create: `src/pickel/shared/event_envelope.py`
- Modify: `src/pickel/hooks/events.py:19-25`
- Test: `tests/shared/test_event_envelope.py`

**Interfaces:**
- Consumes: 无
- Produces: `EventIdentity(event_id: str, session_id: str, turn_id: str, step_index: int | None, occurred_at: datetime)`；`EventEnvelope(EventIdentity)` 额外带 `seq: int`（默认 `-1` 表示未分配）；`EventEnvelope.with_seq(seq: int) -> EventEnvelope`

**为什么放 `shared/`：** `runs/strategy/react.py:88` import `hooks`，所以 `hooks` 不能反向 import `runs`，否则循环。信封是两者共用的，必须放在更底层的包。

**为什么分两层：** hook 事件不经过 EventBus，没有分配者，`seq` 对它无意义。分层让 hook 侧不必背一个恒为 `-1` 的字段。

- [ ] **Step 1: 写失败测试**

```python
# tests/shared/test_event_envelope.py
"""事件信封：hook 与 runtime 共用的身份字段。"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

from pickel.hooks.events import PreToolUseEvent
from pickel.shared.event_envelope import EventEnvelope, EventIdentity


def test_identity_字段齐全且有默认值():
    identity = EventIdentity()

    assert identity.event_id
    assert identity.session_id == ""
    assert identity.turn_id == ""
    assert identity.step_index is None
    assert identity.occurred_at.tzinfo is timezone.utc


def test_每个_identity_的_event_id_唯一():
    assert EventIdentity().event_id != EventIdentity().event_id


def test_envelope_默认_seq_为未分配():
    assert EventEnvelope().seq == -1


def test_with_seq_返回新实例且不改原件():
    original = EventEnvelope()
    assigned = original.with_seq(7)

    assert assigned.seq == 7
    assert original.seq == -1
    assert assigned.event_id == original.event_id


def test_envelope_是_frozen():
    envelope = EventEnvelope()
    try:
        envelope.seq = 3  # type: ignore[misc]
    except Exception as exc:
        assert type(exc).__name__ == "FrozenInstanceError"
    else:
        raise AssertionError("EventEnvelope 必须是 frozen")


def test_hook_事件继承_identity():
    """hook 事件复用同一组身份字段，但不带 seq。"""
    event = PreToolUseEvent(session_id="s1", turn_id="t1", tool_name="echo")

    assert isinstance(event, EventIdentity)
    assert event.session_id == "s1"
    assert not hasattr(event, "seq")


def test_hook_事件仍可正常构造与_replace():
    """确认改基类没破坏既有 hook 用法。"""
    event = PreToolUseEvent(
        session_id="s1", turn_id="t1", step_index=2,
        tool_name="echo", tool_call_id="c1", arguments={"text": "x"},
    )
    updated = replace(event, step_index=3)

    assert updated.step_index == 3
    assert updated.tool_name == "echo"
    assert isinstance(updated.occurred_at, datetime)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `GEMINI_API_KEY=fake uv run --with pytest pytest tests/shared/test_event_envelope.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'pickel.shared.event_envelope'`

- [ ] **Step 3: 实现信封**

```python
# src/pickel/shared/event_envelope.py
"""事件信封：hook 与 runtime 事件共用的身份字段。

放在 shared/ 而非 runs/：runs 依赖 hooks（react 调 lifecycle_hooks），
hooks 若反向依赖 runs 会形成循环。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from uuid import uuid4


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class EventIdentity:
    """一个事件是谁、属于哪个 turn、发生在何时。"""

    event_id: str = field(default_factory=lambda: str(uuid4()))
    session_id: str = ""
    turn_id: str = ""
    step_index: int | None = None
    occurred_at: datetime = field(default_factory=_now)


@dataclass(frozen=True)
class EventEnvelope(EventIdentity):
    """runtime 事件的信封：身份 + 全序序号。

    seq 由 EventBus 统一分配（红线 4）；-1 表示尚未进入 bus。
    hook 事件不经过 bus，故只用 EventIdentity，不背这个字段。
    """

    seq: int = -1

    def with_seq(self, seq: int) -> "EventEnvelope":
        return replace(self, seq=seq)
```

- [ ] **Step 4: 让 hook 事件继承 EventIdentity**

`src/pickel/hooks/events.py` 顶部改为：

```python
"""Hook 事件 DTO（只读快照）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pickel.shared.event_envelope import EventIdentity

if TYPE_CHECKING:
    from pickel.context.model_context import ModelContext


@dataclass(frozen=True)
class HookEventBase(EventIdentity):
    """向后兼容的别名基类；身份字段全部来自 EventIdentity。"""
```

删掉原 `hooks/events.py:14-25` 的 `_now()` 与 `HookEventBase` 的五个字段定义，以及不再需要的 `datetime`/`timezone`/`uuid4` import。其余事件类（`UserPromptSubmitEvent` 等）不动，仍继承 `HookEventBase`。

- [ ] **Step 5: 跑测试确认通过**

Run: `GEMINI_API_KEY=fake uv run --with pytest pytest tests/shared/test_event_envelope.py tests/hooks/ -q`
Expected: PASS，hooks 既有测试全绿

- [ ] **Step 6: 提交**

```bash
git checkout uv.lock
git add src/pickel/shared/event_envelope.py src/pickel/hooks/events.py tests/shared/test_event_envelope.py
git commit -m "feat(events): 抽出 EventIdentity/EventEnvelope，hook 事件复用身份字段"
```

---

## Task 2: tagged union 事件类型

**Files:**
- Create: `src/pickel/runs/runtime_events.py`
- Test: `tests/runs/test_runtime_events.py`

**Interfaces:**
- Consumes: `EventEnvelope`（Task 1）
- Produces: 基类 `RuntimeEventBase(envelope: EventEnvelope)` 带 `EVENT_TYPE: ClassVar[str]` 与 `to_dict() -> dict`；事件类 `TurnStarted` / `StepStarted` / `ToolCallStarted` / `ToolCallCompleted` / `AssistantMessageEvent` / `TurnCompleted` / `TurnFailed`；类型别名 `RuntimeEventHandler = Callable[[RuntimeEventBase], Awaitable[None] | None]`

**取代关系：** 旧 `runs/events.py:13-14` 的 `TOOL_CALL_FAILED` 并入 `ToolCallCompleted`——失败信息已在 `ToolExecutionResult.is_error`，两个类型表达同一件事。

**序列化的坑：** `ToolCall.thought_signature` 是 `bytes | None`（`conversations/message.py:38`），直接进 `json.dumps` 会抛 `TypeError`。`to_dict` 必须转 base64 字符串，否则 trace 在带 thought_signature 的 gemini 响应上直接崩。

**E2 预留：** `ThinkingDelta` / `TextDelta` / `ToolCallDelta` / `TurnInterrupted` 不在本任务——E1 不引入未被任何代码发射的事件类型。

- [ ] **Step 1: 写失败测试**

```python
# tests/runs/test_runtime_events.py
"""runtime 事件：tagged union + 可 JSON 序列化。"""

from __future__ import annotations

import json

from pickel.conversations.message import ToolCall
from pickel.runs.runtime_events import (
    AssistantMessageEvent,
    RuntimeEventBase,
    StepStarted,
    ToolCallCompleted,
    ToolCallStarted,
    TurnCompleted,
    TurnFailed,
    TurnStarted,
)
from pickel.runs.turn_usage import TurnUsage
from pickel.shared.event_envelope import EventEnvelope
from pickel.tools.base import ToolExecutionResult


def _envelope() -> EventEnvelope:
    return EventEnvelope(session_id="s1", turn_id="t1", step_index=1, seq=3)


def test_每个事件类型有唯一的_event_type():
    types = [
        TurnStarted, StepStarted, ToolCallStarted, ToolCallCompleted,
        AssistantMessageEvent, TurnCompleted, TurnFailed,
    ]
    values = [cls.EVENT_TYPE for cls in types]

    assert len(set(values)) == len(values)
    assert all(isinstance(v, str) and v for v in values)


def test_to_dict_含信封与_event_type():
    event = StepStarted(envelope=_envelope())
    data = event.to_dict()

    assert data["event_type"] == "step_started"
    assert data["seq"] == 3
    assert data["session_id"] == "s1"
    assert data["turn_id"] == "t1"
    assert data["step_index"] == 1
    assert "event_id" in data
    assert "occurred_at" in data


def test_occurred_at_序列化为_iso_字符串():
    data = StepStarted(envelope=_envelope()).to_dict()

    assert isinstance(data["occurred_at"], str)
    assert "T" in data["occurred_at"]


def test_所有事件都能_json_序列化():
    events: list[RuntimeEventBase] = [
        TurnStarted(envelope=_envelope(), user_text="hi"),
        StepStarted(envelope=_envelope()),
        ToolCallStarted(
            envelope=_envelope(),
            tool_call=ToolCall(id="c1", name="echo", arguments={"text": "x"}),
            batch_id="b1", call_index=0, total_calls=2,
        ),
        ToolCallCompleted(
            envelope=_envelope(),
            tool_call=ToolCall(id="c1", name="echo", arguments={"text": "x"}),
            tool_result=ToolExecutionResult(content="x"),
            batch_id="b1", call_index=0, total_calls=2,
        ),
        AssistantMessageEvent(envelope=_envelope(), text="done"),
        TurnCompleted(envelope=_envelope(), usage=TurnUsage(steps=1), elapsed_ms=120),
        TurnFailed(envelope=_envelope(), error_type="ValueError", message="boom"),
    ]

    for event in events:
        json.dumps(event.to_dict())  # 不抛异常即通过


def test_thought_signature_为_bytes_时仍可序列化():
    """gemini 的 tool_call 带 bytes 签名，直接 json.dumps 会抛 TypeError。"""
    event = ToolCallStarted(
        envelope=_envelope(),
        tool_call=ToolCall(
            id="c1", name="echo", arguments={}, thought_signature=b"\x00\x01\xff"
        ),
        batch_id="b1", call_index=0, total_calls=1,
    )

    data = event.to_dict()
    json.dumps(data)
    assert isinstance(data["tool_call"]["thought_signature"], str)


def test_tool_call_completed_携带失败信息():
    """失败不再是独立事件类型，读 is_error 即可。"""
    event = ToolCallCompleted(
        envelope=_envelope(),
        tool_call=ToolCall(id="c1", name="missing", arguments={}),
        tool_result=ToolExecutionResult(content="not found", is_error=True),
        batch_id="b1", call_index=0, total_calls=1,
    )

    assert event.to_dict()["tool_result"]["is_error"] is True


def test_turn_completed_携带_usage_合计():
    usage = TurnUsage(steps=2, input_tokens=100, cache_read_tokens=5, output_tokens=20)
    data = TurnCompleted(envelope=_envelope(), usage=usage, elapsed_ms=300).to_dict()

    assert data["usage"]["steps"] == 2
    assert data["usage"]["actual_input_tokens"] == 105


def test_turn_failed_不携带_traceback_到_dict_之外的地方():
    event = TurnFailed(
        envelope=_envelope(), error_type="ValueError",
        message="boom", traceback_text="line1\nline2",
    )
    data = event.to_dict()

    assert data["error_type"] == "ValueError"
    assert data["traceback"] == "line1\nline2"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `GEMINI_API_KEY=fake uv run --with pytest pytest tests/runs/test_runtime_events.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'pickel.runs.runtime_events'`

- [ ] **Step 3: 实现事件类型**

```python
# src/pickel/runs/runtime_events.py
"""Runtime 事件：tagged union，每个时机一个类型。

与 hook 事件的区别：这些是 fire-and-forget 广播，订阅者只读，
不得改写 agent 行为（设计红线 8）。
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, ClassVar, TypeAlias

from pickel.conversations.message import ToolCall
from pickel.runs.turn_usage import TurnUsage
from pickel.shared.event_envelope import EventEnvelope
from pickel.tools.base import ToolExecutionResult


def _tool_call_to_dict(tool_call: ToolCall) -> dict[str, Any]:
    """thought_signature 是 bytes，必须转 base64 才能 json.dumps。"""
    signature = tool_call.thought_signature
    return {
        "id": tool_call.id,
        "name": tool_call.name,
        "arguments": tool_call.arguments,
        "thought_signature": (
            base64.b64encode(signature).decode("ascii")
            if signature is not None
            else None
        ),
    }


def _tool_result_to_dict(result: ToolExecutionResult) -> dict[str, Any]:
    return {
        "content": result.content,
        "is_error": result.is_error,
        "metadata": result.metadata,
    }


def _usage_to_dict(usage: TurnUsage) -> dict[str, Any]:
    return {
        "steps": usage.steps,
        "input_tokens": usage.input_tokens,
        "cache_read_tokens": usage.cache_read_tokens,
        "cache_write_tokens": usage.cache_write_tokens,
        "output_tokens": usage.output_tokens,
        "actual_input_tokens": usage.actual_input_tokens,
        "elapsed_ms": usage.elapsed_ms,
        "hook_injected_chars": usage.hook_injected_chars,
        "model_label": usage.model_label,
    }


@dataclass(frozen=True)
class RuntimeEventBase:
    EVENT_TYPE: ClassVar[str] = ""

    envelope: EventEnvelope = field(default_factory=EventEnvelope)

    def to_dict(self) -> dict[str, Any]:
        envelope = self.envelope
        base = {
            "event_type": self.EVENT_TYPE,
            "event_id": envelope.event_id,
            "session_id": envelope.session_id,
            "turn_id": envelope.turn_id,
            "step_index": envelope.step_index,
            "seq": envelope.seq,
            "occurred_at": envelope.occurred_at.isoformat(),
        }
        base.update(self._payload())
        return base

    def _payload(self) -> dict[str, Any]:
        return {}


@dataclass(frozen=True)
class TurnStarted(RuntimeEventBase):
    EVENT_TYPE: ClassVar[str] = "turn_started"

    user_text: str = ""

    def _payload(self) -> dict[str, Any]:
        return {"user_text": self.user_text}


@dataclass(frozen=True)
class StepStarted(RuntimeEventBase):
    EVENT_TYPE: ClassVar[str] = "step_started"


@dataclass(frozen=True)
class ToolCallStarted(RuntimeEventBase):
    EVENT_TYPE: ClassVar[str] = "tool_call_started"

    tool_call: ToolCall | None = None
    batch_id: str = ""
    call_index: int = 0
    total_calls: int = 0

    def _payload(self) -> dict[str, Any]:
        return {
            "tool_call": _tool_call_to_dict(self.tool_call) if self.tool_call else None,
            "batch_id": self.batch_id,
            "call_index": self.call_index,
            "total_calls": self.total_calls,
        }


@dataclass(frozen=True)
class ToolCallCompleted(RuntimeEventBase):
    """成功与失败共用；失败读 tool_result.is_error。"""

    EVENT_TYPE: ClassVar[str] = "tool_call_completed"

    tool_call: ToolCall | None = None
    tool_result: ToolExecutionResult | None = None
    batch_id: str = ""
    call_index: int = 0
    total_calls: int = 0

    def _payload(self) -> dict[str, Any]:
        return {
            "tool_call": _tool_call_to_dict(self.tool_call) if self.tool_call else None,
            "tool_result": (
                _tool_result_to_dict(self.tool_result) if self.tool_result else None
            ),
            "batch_id": self.batch_id,
            "call_index": self.call_index,
            "total_calls": self.total_calls,
        }


@dataclass(frozen=True)
class AssistantMessageEvent(RuntimeEventBase):
    EVENT_TYPE: ClassVar[str] = "assistant_message"

    text: str = ""
    usage: TurnUsage | None = None

    def _payload(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "usage": _usage_to_dict(self.usage) if self.usage else None,
        }


@dataclass(frozen=True)
class TurnCompleted(RuntimeEventBase):
    EVENT_TYPE: ClassVar[str] = "turn_completed"

    usage: TurnUsage | None = None
    elapsed_ms: int = 0

    def _payload(self) -> dict[str, Any]:
        return {
            "usage": _usage_to_dict(self.usage) if self.usage else None,
            "elapsed_ms": self.elapsed_ms,
        }


@dataclass(frozen=True)
class TurnFailed(RuntimeEventBase):
    EVENT_TYPE: ClassVar[str] = "turn_failed"

    error_type: str = ""
    message: str = ""
    traceback_text: str = ""

    def _payload(self) -> dict[str, Any]:
        return {
            "error_type": self.error_type,
            "message": self.message,
            "traceback": self.traceback_text,
        }


RuntimeEventHandler: TypeAlias = Callable[
    [RuntimeEventBase], Awaitable[None] | None
]
```

- [ ] **Step 4: 跑测试确认通过**

Run: `GEMINI_API_KEY=fake uv run --with pytest pytest tests/runs/test_runtime_events.py -q`
Expected: PASS（9 个）

- [ ] **Step 5: 提交**

```bash
git checkout uv.lock
git add src/pickel/runs/runtime_events.py tests/runs/test_runtime_events.py
git commit -m "feat(events): tagged union 事件类型，to_dict 处理 bytes 签名"
```

---

## Task 3: EventBus

**Files:**
- Create: `src/pickel/runs/event_bus.py`
- Test: `tests/runs/test_event_bus.py`

**Interfaces:**
- Consumes: `RuntimeEventBase`、`RuntimeEventHandler`（Task 2）
- Produces: `EventBus()` 带 `subscribe(handler) -> Callable[[], None]`（返回退订函数）与 `async emit(event: RuntimeEventBase) -> RuntimeEventBase`（返回已分配 seq 的事件）

**关键行为：** 订阅者抛异常必须被吞掉并继续分发给其余订阅者。UI 渲染崩溃不该杀掉正在跑的 turn（红线 2）。

- [ ] **Step 1: 写失败测试**

```python
# tests/runs/test_event_bus.py
"""EventBus：seq 全序分配 + 订阅者异常隔离。"""

from __future__ import annotations

import asyncio

from pickel.runs.event_bus import EventBus
from pickel.runs.runtime_events import StepStarted, TurnStarted
from pickel.shared.event_envelope import EventEnvelope


def test_seq_从_0_起严格递增():
    bus = EventBus()
    seen = []
    bus.subscribe(lambda event: seen.append(event.envelope.seq))

    asyncio.run(_emit_many(bus, 3))

    assert seen == [0, 1, 2]


async def _emit_many(bus: EventBus, count: int) -> None:
    for _ in range(count):
        await bus.emit(StepStarted(envelope=EventEnvelope(session_id="s1")))


def test_emit_返回带_seq_的事件且不改原件():
    bus = EventBus()
    original = StepStarted(envelope=EventEnvelope(session_id="s1"))

    emitted = asyncio.run(bus.emit(original))

    assert emitted.envelope.seq == 0
    assert original.envelope.seq == -1


def test_多订阅者都收到同一事件():
    bus = EventBus()
    a, b = [], []
    bus.subscribe(lambda e: a.append(e))
    bus.subscribe(lambda e: b.append(e))

    asyncio.run(bus.emit(TurnStarted(user_text="hi")))

    assert len(a) == 1 and len(b) == 1
    assert a[0].envelope.seq == b[0].envelope.seq


def test_订阅者抛异常不影响其余订阅者():
    bus = EventBus()
    survivors = []

    def explode(event):
        raise RuntimeError("renderer crashed")

    bus.subscribe(explode)
    bus.subscribe(lambda e: survivors.append(e))

    asyncio.run(bus.emit(StepStarted()))

    assert len(survivors) == 1


def test_唯一订阅者抛异常时_emit_仍正常返回():
    """红线 2：UI 崩了不该杀掉正在跑的 turn。"""
    bus = EventBus()

    def explode(event):
        raise RuntimeError("boom")

    bus.subscribe(explode)

    emitted = asyncio.run(bus.emit(StepStarted()))

    assert emitted.envelope.seq == 0


def test_异步订阅者被_await():
    bus = EventBus()
    seen = []

    async def handler(event):
        await asyncio.sleep(0)
        seen.append(event)

    bus.subscribe(handler)
    asyncio.run(bus.emit(StepStarted()))

    assert len(seen) == 1


def test_异步订阅者抛异常同样被隔离():
    bus = EventBus()
    survivors = []

    async def explode(event):
        raise RuntimeError("async crash")

    async def keep(event):
        survivors.append(event)

    bus.subscribe(explode)
    bus.subscribe(keep)
    asyncio.run(bus.emit(StepStarted()))

    assert len(survivors) == 1


def test_退订后不再收到事件():
    bus = EventBus()
    seen = []
    unsubscribe = bus.subscribe(lambda e: seen.append(e))

    asyncio.run(bus.emit(StepStarted()))
    unsubscribe()
    asyncio.run(bus.emit(StepStarted()))

    assert len(seen) == 1


def test_无订阅者时_emit_仍分配_seq():
    bus = EventBus()

    first = asyncio.run(bus.emit(StepStarted()))
    second = asyncio.run(bus.emit(StepStarted()))

    assert (first.envelope.seq, second.envelope.seq) == (0, 1)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `GEMINI_API_KEY=fake uv run --with pytest pytest tests/runs/test_event_bus.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'pickel.runs.event_bus'`

- [ ] **Step 3: 实现 EventBus**

```python
# src/pickel/runs/event_bus.py
"""EventBus：runtime 事件的多订阅广播。

两条硬约束：
1. seq 只在这里分配（红线 4）——它是全序的唯一来源
2. 订阅者异常被吞掉（红线 2）——渲染器崩溃不得杀掉正在跑的 turn
"""

from __future__ import annotations

import inspect
import logging
from dataclasses import replace
from typing import Callable

from pickel.runs.runtime_events import RuntimeEventBase, RuntimeEventHandler

logger = logging.getLogger(__name__)


class EventBus:
    def __init__(self) -> None:
        self._handlers: list[RuntimeEventHandler] = []
        self._next_seq = 0

    def subscribe(self, handler: RuntimeEventHandler) -> Callable[[], None]:
        """注册订阅者，返回退订函数。"""
        self._handlers.append(handler)

        def unsubscribe() -> None:
            if handler in self._handlers:
                self._handlers.remove(handler)

        return unsubscribe

    async def emit(self, event: RuntimeEventBase) -> RuntimeEventBase:
        """分配 seq 后广播；返回带 seq 的事件。"""
        stamped = replace(event, envelope=event.envelope.with_seq(self._next_seq))
        self._next_seq += 1

        for handler in list(self._handlers):
            try:
                result = handler(stamped)
                if inspect.isawaitable(result):
                    await result
            except Exception:  # noqa: BLE001 — 订阅者异常不得影响 turn
                logger.exception("事件订阅者异常，已隔离: %s", type(handler).__name__)
        return stamped
```

- [ ] **Step 4: 跑测试确认通过**

Run: `GEMINI_API_KEY=fake uv run --with pytest pytest tests/runs/test_event_bus.py -q`
Expected: PASS（9 个）

- [ ] **Step 5: 提交**

```bash
git checkout uv.lock
git add src/pickel/runs/event_bus.py tests/runs/test_event_bus.py
git commit -m "feat(events): EventBus 分配 seq 并隔离订阅者异常"
```

---

## Task 4: react 切换到新事件，删死代码

**Files:**
- Modify: `src/pickel/runs/strategy/react.py`（发射点 + 删 `_execute_tool_batch`/`_execute_one`）
- Modify: `src/pickel/runs/strategy/base.py:16`（删重复的 `RuntimeEventHandler`）
- Modify: `src/pickel/runs/run.py:137-180`（`turn` 接 bus）
- Modify: `src/pickel/runs/__init__.py`
- Test: `tests/runs/test_events.py`（改写现有两个测试）

**Interfaces:**
- Consumes: `EventBus`（Task 3）、事件类型（Task 2）
- Produces: `ExecutionStrategy.execute(run, session, bus: EventBus | None = None, turn_id: str | None = None, initial_hook_feedback=None)`；`Run.turn(session, user_text, bus: EventBus | None = None)`

`turn_id` 参数在本任务一次加到位（Task 5 的 turn 级事件要靠它让 `TurnStarted` 与 step 事件共享同一个 id），本任务先不传，保持 `None` 时自生成的原行为。

**死代码：** `react.py:328-418` 的 `_execute_tool_batch` / `_execute_one` 与 `ToolExecutionOutcome` 零调用点（grep 全仓含测试均无）。它们含一份重复的事件发射逻辑，留着会让本次改造要改两遍且第二遍永远不被执行。删掉。

**重复定义：** `RuntimeEventHandler` 在 `runs/events.py:31` 与 `strategy/base.py:16` 各定义一次且签名不同（`Awaitable[None] | None` vs `None | object`）。删 base.py 的，统一 import Task 2 的。

- [ ] **Step 1: 改写现有事件测试**

把 `tests/runs/test_events.py` 的两个测试改为新 API。`StubProvider`、`DelayEchoTool`、`_run`、`_assistant_text` 全部保持原样不动，只改 import 与两个测试体：

```python
# tests/runs/test_events.py 顶部 import 改为
from pickel.runs import ReActStrategy, Run
from pickel.runs.event_bus import EventBus
from pickel.runs.runtime_events import (
    AssistantMessageEvent,
    StepStarted,
    ToolCallCompleted,
    ToolCallStarted,
)
```

```python
    async def test_runner_emits_batch_aware_events_for_started_and_completed_calls(self) -> None:
        # ... agent / run / session 构造保持原样 ...
        session = Session.create(agent_id="Pickle", session_id="session-1")
        bus = EventBus()
        events = []
        bus.subscribe(lambda event: events.append(event))

        result = await run.turn(session=session, user_text="hello", bus=bus)

        self.assertEqual("done", _assistant_text(result))
        # 工具串行执行以保留 PreToolUse 控制点
        self.assertEqual(
            [
                StepStarted, ToolCallStarted, ToolCallCompleted,
                ToolCallStarted, ToolCallCompleted,
                StepStarted, AssistantMessageEvent,
            ],
            [type(event) for event in _without_turn_events(events)],
        )
        step_events = _without_turn_events(events)
        batch_id = step_events[1].batch_id
        self.assertTrue(batch_id)
        self.assertEqual(batch_id, step_events[2].batch_id)
        self.assertEqual(0, step_events[1].call_index)
        self.assertEqual("slow", step_events[2].tool_result.content)
        self.assertEqual(1, step_events[3].call_index)
        self.assertEqual("fast", step_events[4].tool_result.content)
        self.assertEqual("done", step_events[6].text)

    async def test_runner_emits_completed_event_with_is_error_for_failing_call(self) -> None:
        # ... agent / run / session 构造保持原样 ...
        session = Session.create(agent_id="Pickle", session_id="session-1")
        bus = EventBus()
        events = []
        bus.subscribe(lambda event: events.append(event))

        await run.turn(session=session, user_text="hello", bus=bus)

        failure = next(
            event
            for event in events
            if isinstance(event, ToolCallCompleted) and event.tool_result.is_error
        )
        self.assertEqual("missing", failure.tool_call.name)

    async def test_每个事件都带_session_id_turn_id_与递增_seq(self) -> None:
        """信封必须一路贯通到发射点，否则事件出不了进程。"""
        # ... 复用第一个测试的 agent/run 构造，tools=[DelayEchoTool()] ...
        session = Session.create(agent_id="Pickle", session_id="session-1")
        bus = EventBus()
        events = []
        bus.subscribe(lambda event: events.append(event))

        await run.turn(session=session, user_text="hello", bus=bus)

        self.assertTrue(events)
        self.assertEqual(
            list(range(len(events))), [e.envelope.seq for e in events]
        )
        self.assertTrue(all(e.envelope.session_id == "session-1" for e in events))
        turn_ids = {e.envelope.turn_id for e in events}
        self.assertEqual(1, len(turn_ids))
        self.assertTrue(next(iter(turn_ids)))
```

在文件底部 `if __name__` 之前加辅助函数：

```python
def _without_turn_events(events):
    """滤掉 turn 级事件，只看 step 内序列。"""
    from pickel.runs.runtime_events import TurnCompleted, TurnStarted

    return [e for e in events if not isinstance(e, (TurnStarted, TurnCompleted))]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `GEMINI_API_KEY=fake uv run --with pytest pytest tests/runs/test_events.py -q`
Expected: FAIL — `TypeError: turn() got an unexpected keyword argument 'bus'`

- [ ] **Step 3: 改 `strategy/base.py`**

删掉 `base.py:9` 的 `from pickel.runs.events import RuntimeEvent` 和 `base.py:16` 的 `RuntimeEventHandler = Callable[...]`，改为：

```python
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from pickel.context.hook_feedback import HookFeedback
from pickel.conversations.agent_message import AssistantMessage
from pickel.conversations.session import Session

if TYPE_CHECKING:
    from pickel.runs.event_bus import EventBus
    from pickel.runs.run import Run


class ExecutionStrategy(ABC):
    """Agent 执行策略基类。"""

    @abstractmethod
    async def execute(
        self,
        run: Run,
        session: Session,
        bus: "EventBus | None" = None,
        turn_id: str | None = None,
        initial_hook_feedback: list[HookFeedback] | None = None,
    ) -> AssistantMessage:
        """推进 turn 内 step 循环，返回最终 AssistantMessage。

        turn_id 为 None 时自生成；由 Run.turn 传入可让 turn 级事件
        与 step 事件共享同一个 id（Task 5）。
        """
        raise NotImplementedError
```

- [ ] **Step 4: 改 `react.py` 的发射点**

import 段：删 `from pickel.runs.events import RuntimeEvent, RuntimeEventType` 与 `from pickel.runs.strategy.base import ExecutionStrategy, RuntimeEventHandler`，改为：

```python
from pickel.runs.event_bus import EventBus
from pickel.runs.runtime_events import (
    AssistantMessageEvent,
    StepStarted,
    ToolCallCompleted,
    ToolCallStarted,
)
from pickel.runs.strategy.base import ExecutionStrategy
from pickel.shared.event_envelope import EventEnvelope
```

`execute` 签名把 `event_handler: RuntimeEventHandler | None = None` 换成 `bus: EventBus | None = None`，并加 `turn_id: str | None = None`。在 `turn = TurnState()` 之后加一个信封工厂：

```python
        turn = TurnState() if turn_id is None else TurnState(turn_id=turn_id)

        def envelope(step_index: int | None = None) -> EventEnvelope:
            return EventEnvelope(
                session_id=session.session_id,
                turn_id=turn.turn_id,
                step_index=step_index,
            )
```

四个发射点逐一替换（行号为改动前）：

`react.py:64-70` →
```python
            await self._emit(bus, StepStarted(envelope=envelope(step_index)))
```

`react.py:142-150` →
```python
                await self._emit(
                    bus,
                    AssistantMessageEvent(
                        envelope=envelope(step_index),
                        text=text,
                        usage=last_turn_usage(session),
                    ),
                )
```

`react.py:172-182` →
```python
                await self._emit(
                    bus,
                    ToolCallStarted(
                        envelope=envelope(step_index),
                        tool_call=runtime_call,
                        batch_id=batch_id,
                        call_index=call_index,
                        total_calls=len(tool_calls),
                    ),
                )
```

`react.py:230-245` →
```python
                await self._emit(
                    bus,
                    ToolCallCompleted(
                        envelope=envelope(step_index),
                        tool_call=runtime_call,
                        tool_result=result,
                        batch_id=batch_id,
                        call_index=call_index,
                        total_calls=len(tool_calls),
                    ),
                )
```

`react.py:289-297`（max steps）→
```python
        await self._emit(
            bus,
            AssistantMessageEvent(
                envelope=envelope(self.max_steps),
                text="Reached the maximum number of reasoning steps.",
                usage=last_turn_usage(session),
            ),
        )
```

`_emit_event` 替换为：

```python
    @staticmethod
    async def _emit(bus: EventBus | None, event) -> None:
        if bus is None:
            return
        await bus.emit(event)
```

顶部加 `from pickel.runs.turn_usage import last_turn_usage`。

同时删掉两处已确认无其它用途的代码：

- `react.py:484` 的 `_to_message_metadata`——它的两个调用点（`:148`、`:295`）在本步骤都已改为传 `usage=last_turn_usage(session)`，删除后连带删掉 `MessageMetadata` 的 import
- `react.py:6` 的 `import inspect`——`:510` 的 `_emit_event` 是唯一使用者，已被 `_emit` 取代

删完确认：

```bash
grep -n "_to_message_metadata\|MessageMetadata\|inspect" src/pickel/runs/strategy/react.py
```

Expected: 无输出。

- [ ] **Step 5: 删死代码**

删除 `react.py:328-418` 的 `_execute_tool_batch` 与 `_execute_one` 两个方法，以及 `ToolExecutionOutcome` 的定义与 import（在 `react.py` 顶部）。

删除前确认零引用：

```bash
grep -rn "_execute_tool_batch\|_execute_one\|ToolExecutionOutcome" src tests
```

Expected: 只剩定义处自身；若有其它引用，停下来报告。

- [ ] **Step 6: 改 `run.py` 的 turn**

`run.py:137-180`：把 `event_handler: RuntimeEventHandler | None = None` 换成 `bus: EventBus | None = None`，`strategy.execute(...)` 的传参同步改为 `bus=bus`。import 段把 `from pickel.runs.events import RuntimeEventHandler` 换成：

```python
if TYPE_CHECKING:
    from pickel.runs.event_bus import EventBus
```

若 `run.py` 尚无 `TYPE_CHECKING` import，加 `from typing import TYPE_CHECKING`。

- [ ] **Step 7: 改 `runs/__init__.py` 导出**

```python
from pickel.runs.event_bus import EventBus
from pickel.runs.runtime_events import (
    AssistantMessageEvent,
    RuntimeEventBase,
    RuntimeEventHandler,
    StepStarted,
    ToolCallCompleted,
    ToolCallStarted,
    TurnCompleted,
    TurnFailed,
    TurnStarted,
)
from pickel.shared.generation import (
    FinishReason,
    GenerateRequest,
    GenerateResult,
    TokenUsage,
)

__all__ = [
    "AssistantMessageEvent",
    "EventBus",
    "ExecutionStrategy",
    "FinishReason",
    "GenerateRequest",
    "GenerateResult",
    "ReActStrategy",
    "Run",
    "RuntimeEventBase",
    "RuntimeEventHandler",
    "StepStarted",
    "TokenUsage",
    "ToolCallCompleted",
    "ToolCallStarted",
    "TurnCompleted",
    "TurnFailed",
    "TurnStarted",
]
```

`__getattr__` 部分保持原样不动。

- [ ] **Step 8: 跑测试确认通过**

Run: `GEMINI_API_KEY=fake uv run --with pytest pytest tests/runs/ -q`
Expected: PASS。`tests/cli/test_chat_loop.py` 此时会失败（Task 7 修），先只跑 `tests/runs/`。

- [ ] **Step 9: 提交**

```bash
git checkout uv.lock
git add -A
git commit -m "refactor(events): react/run 切到 EventBus 与 tagged union，删死代码"
```

---

## Task 5: turn 级事件

**Files:**
- Modify: `src/pickel/runs/run.py:137-180`
- Test: `tests/runs/test_turn_events.py`

**Interfaces:**
- Consumes: `TurnStarted` / `TurnCompleted` / `TurnFailed`（Task 2）、`EventBus`（Task 3）
- Produces: 无新接口——`Run.turn` 在原有边界上补发三个事件

**为什么在 `run.turn` 而非 `react.execute`：** turn 边界属于 `Run`（`run.py:144` 的注释已如此界定），且 `turn_id` 目前由 `TurnState` 在 `react` 内生成。本任务把 `turn_id` 上提到 `run.turn` 生成并传给 strategy，让 `TurnStarted` 与后续 step 事件共享同一个 `turn_id`——否则 `TurnStarted` 拿不到 id，事件流无法按 turn 分组。

- [ ] **Step 1: 写失败测试**

```python
# tests/runs/test_turn_events.py
"""turn 级事件：started / completed / failed。"""

from __future__ import annotations

import unittest
from pathlib import Path

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
from pickel.runs import ReActStrategy, Run
from pickel.runs.event_bus import EventBus
from pickel.runs.runtime_events import TurnCompleted, TurnFailed, TurnStarted
from pickel.shared.model_config import ModelConfig
from pickel.tools.shell import ShellSessionManager


class _Provider:
    def __init__(self, reply=None, error=None):
        self.reply = reply
        self.error = error

    async def generate(self, context: ModelContext) -> AssistantMessage:
        if self.error is not None:
            raise self.error
        return self.reply


def _reply() -> AssistantMessage:
    return AssistantMessage(
        content=[TextContent(text="done")],
        metadata=ModelResponseMetadata(
            provider="fake", model="fake-1",
            usage=ModelUsage(input_tokens=100, output_tokens=10),
        ),
    )


def _run(provider) -> Run:
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
        tools=[],
        context_assembler=ContextAssembler(),
        lifecycle_hooks=NoopLifecycleHooks(),
        session_service=None,
        file_access_policy=None,
        workspace_files=None,
        shell_session_manager=ShellSessionManager(),
        unit_window=5,
        strategy=ReActStrategy(max_steps=2),
    )


class TurnEventTests(unittest.IsolatedAsyncioTestCase):
    async def test_turn_started_是第一个事件且带_user_text(self) -> None:
        session = Session.create(agent_id="Pickle", session_id="s1")
        bus = EventBus()
        events = []
        bus.subscribe(lambda e: events.append(e))

        await _run(_Provider(reply=_reply())).turn(
            session=session, user_text="hello", bus=bus
        )

        self.assertIsInstance(events[0], TurnStarted)
        self.assertEqual("hello", events[0].user_text)
        self.assertEqual(0, events[0].envelope.seq)

    async def test_turn_completed_是最后一个事件且带_usage(self) -> None:
        session = Session.create(agent_id="Pickle", session_id="s1")
        bus = EventBus()
        events = []
        bus.subscribe(lambda e: events.append(e))

        await _run(_Provider(reply=_reply())).turn(
            session=session, user_text="hello", bus=bus
        )

        self.assertIsInstance(events[-1], TurnCompleted)
        self.assertEqual(1, events[-1].usage.steps)
        self.assertEqual(100, events[-1].usage.input_tokens)
        self.assertGreaterEqual(events[-1].elapsed_ms, 0)

    async def test_同一个_turn_的所有事件共享_turn_id(self) -> None:
        session = Session.create(agent_id="Pickle", session_id="s1")
        bus = EventBus()
        events = []
        bus.subscribe(lambda e: events.append(e))

        await _run(_Provider(reply=_reply())).turn(
            session=session, user_text="hello", bus=bus
        )

        turn_ids = {e.envelope.turn_id for e in events}
        self.assertEqual(1, len(turn_ids))
        self.assertTrue(next(iter(turn_ids)))

    async def test_provider_抛异常时发_turn_failed_并重新抛出(self) -> None:
        session = Session.create(agent_id="Pickle", session_id="s1")
        bus = EventBus()
        events = []
        bus.subscribe(lambda e: events.append(e))

        with self.assertRaises(ValueError):
            await _run(_Provider(error=ValueError("boom"))).turn(
                session=session, user_text="hello", bus=bus
            )

        failed = [e for e in events if isinstance(e, TurnFailed)]
        self.assertEqual(1, len(failed))
        self.assertEqual("ValueError", failed[0].error_type)
        self.assertIn("boom", failed[0].message)
        self.assertIn("ValueError", failed[0].traceback_text)

    async def test_失败时不发_turn_completed(self) -> None:
        session = Session.create(agent_id="Pickle", session_id="s1")
        bus = EventBus()
        events = []
        bus.subscribe(lambda e: events.append(e))

        with self.assertRaises(ValueError):
            await _run(_Provider(error=ValueError("boom"))).turn(
                session=session, user_text="hello", bus=bus
            )

        self.assertFalse([e for e in events if isinstance(e, TurnCompleted)])

    async def test_hook_阻断时不发_turn_failed(self) -> None:
        """阻断是正常结果，不是错误。"""
        from pickel.hooks.decisions import UserPromptSubmitDecision

        class BlockingHooks(NoopLifecycleHooks):
            async def user_prompt_submit(self, event):
                return UserPromptSubmitDecision(action="block", reason="nope")

        run = _run(_Provider(reply=_reply()))
        run.lifecycle_hooks = BlockingHooks()
        session = Session.create(agent_id="Pickle", session_id="s1")
        bus = EventBus()
        events = []
        bus.subscribe(lambda e: events.append(e))

        await run.turn(session=session, user_text="hello", bus=bus)

        self.assertFalse([e for e in events if isinstance(e, TurnFailed)])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `GEMINI_API_KEY=fake uv run --with pytest pytest tests/runs/test_turn_events.py -q`
Expected: FAIL — `events[0]` 不是 `TurnStarted`（当前第一个事件是 `StepStarted`）

- [ ] **Step 3: 在 run.turn 补三个事件**

`run.py` import 段加：

```python
import time
import traceback
from uuid import uuid4

from pickel.runs.runtime_events import TurnCompleted, TurnFailed, TurnStarted
from pickel.runs.turn_usage import last_turn_usage
from pickel.shared.event_envelope import EventEnvelope
```

`turn` 方法改为：

```python
    async def turn(
        self,
        *,
        session: Session,
        user_text: str,
        bus: "EventBus | None" = None,
    ) -> AssistantMessage:
        """turn 边界：UserPromptSubmit hook → 写 user → strategy.execute。"""
        if session.agent_id != self.agent.agent_id:
            raise ValueError(
                f"Session '{session.session_id}' belongs to agent '{session.agent_id}', "
                f"not '{self.agent.agent_id}'"
            )

        turn_id = str(uuid4())

        def envelope() -> EventEnvelope:
            return EventEnvelope(session_id=session.session_id, turn_id=turn_id)

        async def emit(event) -> None:
            if bus is not None:
                await bus.emit(event)

        await emit(TurnStarted(envelope=envelope(), user_text=user_text))
        started = time.perf_counter()

        decision = await self.lifecycle_hooks.user_prompt_submit(
            UserPromptSubmitEvent(
                session_id=session.session_id,
                turn_id=turn_id,
                prompt=user_text,
            )
        )
        if decision.action == "block":
            blocked = AssistantMessage(
                content=[TextContent(text=decision.reason or "请求被 Hook 阻止")]
            )
            await emit(
                TurnCompleted(
                    envelope=envelope(),
                    usage=None,
                    elapsed_ms=round((time.perf_counter() - started) * 1000),
                )
            )
            return blocked

        user_entry = session.append_user(
            UserMessage(content=[TextContent(text=user_text)])
        )
        if self.session_service is not None:
            self.session_service.flush_new_entries(
                session=session,
                entries=[user_entry],
            )

        try:
            reply = await self.strategy.execute(
                run=self,
                session=session,
                bus=bus,
                turn_id=turn_id,
                initial_hook_feedback=(
                    [HookFeedback(source_event="UserPromptSubmit", text=decision.feedback_text)]
                    if decision.feedback_text
                    else None
                ),
            )
        except Exception as exc:
            await emit(
                TurnFailed(
                    envelope=envelope(),
                    error_type=type(exc).__name__,
                    message=str(exc),
                    traceback_text=traceback.format_exc(),
                )
            )
            raise

        await emit(
            TurnCompleted(
                envelope=envelope(),
                usage=last_turn_usage(session),
                elapsed_ms=round((time.perf_counter() - started) * 1000),
            )
        )
        return reply
```

`turn_id` 参数与 `TurnState(turn_id=...)` 构造已在 Task 4 Step 3/4 加好，本任务只需在 `run.turn` 里生成并传入（上方代码已含 `turn_id=turn_id`）。

- [ ] **Step 4: 跑测试确认通过**

Run: `GEMINI_API_KEY=fake uv run --with pytest pytest tests/runs/ -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git checkout uv.lock
git add -A
git commit -m "feat(events): turn_started/completed/failed，turn_id 上提到 Run.turn"
```

---

## Task 6: JSONL trace sink

**Files:**
- Create: `src/pickel/runs/trace_sink.py`
- Modify: `src/pickel/config/loader.py:25-26`（`_BUILTIN_DEFAULTS` 加 `trace_enabled`）
- Modify: `src/pickel/config/app_config.py:57-58`（`AppConfig` 加 `trace_enabled` 字段）
- Test: `tests/runs/test_trace_sink.py`

**Interfaces:**
- Consumes: `RuntimeEventBase.to_dict()`（Task 2）、`EventBus.subscribe`（Task 3）
- Produces: `JsonlTraceSink(path: Path)` 可直接作为订阅者调用（`__call__`），带 `close()`；`trace_enabled(config_value: bool) -> bool`；`trace_path(session_id: str) -> Path`

**配置走既有路径：** `AppConfig` 是 pydantic 模型，没有 settings 原文字段。`loader.py:78-79` 是 `deep_merge(_BUILTIN_DEFAULTS, settings)` 再构造 `AppConfig`，所以加一个扁平键即可，与既有的 `react_max_steps` / `context_cli_turn_window` 同路。

**红线 5/6：** trace 是派生物。本模块只写不读——不提供任何 `load` / `replay_to_session` 函数。默认关闭。

- [ ] **Step 1: 写失败测试**

```python
# tests/runs/test_trace_sink.py
"""JSONL trace：只写不读的派生轨迹（红线 5/6）。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pickel.runs.trace_sink as trace_module
from pickel.runs.event_bus import EventBus
from pickel.runs.runtime_events import StepStarted, TurnStarted
from pickel.runs.trace_sink import JsonlTraceSink, trace_enabled
from pickel.shared.event_envelope import EventEnvelope


def test_默认关闭():
    assert trace_enabled(False) is False


def test_配置可开启():
    assert trace_enabled(True) is True


def test_环境变量覆盖配置(monkeypatch):
    monkeypatch.setenv("PICKEL_TRACE", "1")
    assert trace_enabled(False) is True


def test_环境变量为_0_时关闭即使配置为开(monkeypatch):
    monkeypatch.setenv("PICKEL_TRACE", "0")
    assert trace_enabled(True) is False


def test_app_config_默认_trace_关闭():
    """扁平键走 loader 的 _BUILTIN_DEFAULTS，默认必须是 False。"""
    from pickel.config.loader import _BUILTIN_DEFAULTS

    assert _BUILTIN_DEFAULTS["trace_enabled"] is False


def test_写出的每行都是合法_json(tmp_path: Path):
    path = tmp_path / "s1.jsonl"
    sink = JsonlTraceSink(path)
    bus = EventBus()
    bus.subscribe(sink)

    asyncio.run(_emit(bus))
    sink.close()

    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    assert [json.loads(line)["event_type"] for line in lines] == [
        "turn_started", "step_started",
    ]


async def _emit(bus: EventBus) -> None:
    await bus.emit(TurnStarted(envelope=EventEnvelope(session_id="s1"), user_text="hi"))
    await bus.emit(StepStarted(envelope=EventEnvelope(session_id="s1", step_index=1)))


def test_落盘的_seq_与_bus_分配一致(tmp_path: Path):
    path = tmp_path / "s1.jsonl"
    sink = JsonlTraceSink(path)
    bus = EventBus()
    bus.subscribe(sink)

    asyncio.run(_emit(bus))
    sink.close()

    seqs = [json.loads(line)["seq"] for line in path.read_text().strip().splitlines()]
    assert seqs == [0, 1]


def test_父目录不存在时自动创建(tmp_path: Path):
    path = tmp_path / "nested" / "deep" / "s1.jsonl"
    sink = JsonlTraceSink(path)
    sink(StepStarted())
    sink.close()

    assert path.is_file()


def test_close_后不再写入(tmp_path: Path):
    path = tmp_path / "s1.jsonl"
    sink = JsonlTraceSink(path)
    sink(StepStarted())
    sink.close()

    before = path.read_text(encoding="utf-8")
    try:
        sink(StepStarted())
    except ValueError:
        pass  # 写已关闭的文件句柄

    assert path.read_text(encoding="utf-8") == before


def test_模块不提供任何读回接口():
    """红线 5：trace 是派生物，禁止从中重建对话或用量。"""
    source = Path(trace_module.__file__).read_text(encoding="utf-8")

    assert "def load" not in source
    assert "def replay" not in source
    assert "json.loads" not in source
```

- [ ] **Step 2: 跑测试确认失败**

Run: `GEMINI_API_KEY=fake uv run --with pytest pytest tests/runs/test_trace_sink.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'pickel.runs.trace_sink'`

- [ ] **Step 3: 实现 sink**

```python
# src/pickel/runs/trace_sink.py
"""事件 JSONL 落盘：派生的可观测轨迹。

红线 5：trace 不是对话事实的真源。本模块只写不读——不提供任何
load/replay 接口，禁止任何代码从 trace 重建对话或用量。
真源始终是 Session entry + metadata.usage。

红线 6：默认关闭。工具参数与文件内容会进入 trace。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TextIO

from pickel.config.paths import home_dir
from pickel.runs.runtime_events import RuntimeEventBase

TRACE_ENV_VAR = "PICKEL_TRACE"


def trace_enabled(config_value: bool = False) -> bool:
    """AppConfig.trace_enabled（默认 false），PICKEL_TRACE 覆盖之。"""
    override = os.environ.get(TRACE_ENV_VAR)
    if override is not None:
        return override.strip().lower() in {"1", "true", "yes", "on"}
    return bool(config_value)


def trace_path(session_id: str) -> Path:
    return home_dir() / "traces" / f"{session_id}.jsonl"


class JsonlTraceSink:
    """一行一个事件；作为 EventBus 订阅者直接调用。"""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._handle: TextIO = self._path.open("a", encoding="utf-8")

    def __call__(self, event: RuntimeEventBase) -> None:
        self._handle.write(json.dumps(event.to_dict(), ensure_ascii=False) + "\n")
        self._handle.flush()

    def close(self) -> None:
        if not self._handle.closed:
            self._handle.close()
```

`home_dir()` 无参数，返回 `Path`，`PICKEL_HOME` 可覆盖（`config/paths.py:15-20`）——测试用 tmp_path 直接传 `JsonlTraceSink(path)`，不依赖它。

- [ ] **Step 4: 接入配置**

`src/pickel/config/loader.py` 的 `_BUILTIN_DEFAULTS`（第 25-26 行附近）加一行：

```python
    "trace_enabled": False,
```

`src/pickel/config/app_config.py` 的 `AppConfig`，在 `context_cli_turn_window` 之后加：

```python
    # 事件 JSONL trace；默认关（含工具参数与文件内容）
    trace_enabled: bool = False
```

- [ ] **Step 5: 跑测试确认通过**

Run: `GEMINI_API_KEY=fake uv run --with pytest pytest tests/runs/test_trace_sink.py tests/config/ -q`
Expected: PASS（trace 9 个 + config 既有测试全绿）

- [ ] **Step 6: 提交**

```bash
git checkout uv.lock
git add -A
git commit -m "feat(events): JSONL trace sink，默认关且只写不读"
```

---

## Task 7: CLI 适配

**Files:**
- Modify: `src/pickel/cli/event_renderer.py`
- Modify: `src/pickel/cli/chat.py:111-112`、`:574-593`
- Test: `tests/cli/test_chat_loop.py`（改现有 `/context` 之外的事件相关测试）

**Interfaces:**
- Consumes: 全部事件类型（Task 2、5）、`EventBus`（Task 3）、`JsonlTraceSink`/`trace_enabled`（Task 6）
- Produces: `ChatEventRenderer(console).handle_event(event)` 消费 `RuntimeEventBase`；`ChatLoop.create_event_bus() -> EventBus`

**行为约束（2026-07-26 修订）：** E1 结束时终端的**布局与结构**与改动前一致——Panel 边框、Thinking/Tool/Assistant 三种框、footer 的行数与分隔符格式都不变。新排版属于 E3。

**但 footer 的数字会变，这是设计意图，不是回归：**

| | 改动前 | E1 之后 | 依据 |
|---|---|---|---|
| 输入规模 | 裸 `input_tokens` | `input + cache_read + cache_write` | 可观测性设计 §5.1 强制口径。实测裸 `input_tokens` 低估 250 倍 |
| 统计范围 | 末条 assistant 消息 | 整轮合计（`last_turn_usage`） | 本计划 §4.1：footer 与 `/context` 统一到 `TurnUsage` 一个口径 |

原先写的「逐字节一致」与 Task 5/7 指定的数据源自相矛盾——`last_turn_usage` + `actual_input_tokens` 必然改变数字。让 footer 继续用裸 `input_tokens` 会与 `/context` 显示的数字互相矛盾，那才是缺陷。

**因此测试必须锁住新口径**：至少一条用例带非零 `cache_read_tokens`（否则 `input_tokens` 与 `actual_input_tokens` 恰好相等，断言分辨不出用的是哪个字段），且用完整 footer 字符串的 `assertEqual` 而非 substring `in`。

**顺带消除的重复：** `event_renderer.py:113` 的 `_render_assistant_footer` 改为消费 `TurnUsage`，`chat.py:209` 的同名方法与 `chat.py:179` 的 `_render_assistant_message` 在 Task 7 保留（E3 才删）——但两者不得再各自拼装 `MessageMetadata`。

- [ ] **Step 1: 写失败测试**

```python
# tests/cli/test_event_rendering.py
"""事件渲染：新事件类型下终端输出不变。"""

from __future__ import annotations

import asyncio

from rich.console import Console

from pickel.cli.event_renderer import ChatEventRenderer
from pickel.conversations.message import ToolCall
from pickel.runs.runtime_events import (
    AssistantMessageEvent,
    StepStarted,
    ToolCallCompleted,
    ToolCallStarted,
    TurnCompleted,
    TurnStarted,
)
from pickel.runs.turn_usage import TurnUsage
from pickel.shared.event_envelope import EventEnvelope
from pickel.tools.base import ToolExecutionResult


def _render(event) -> str:
    console = Console(width=100, record=True, force_terminal=False)
    asyncio.run(ChatEventRenderer(console).handle_event(event))
    return console.export_text()


def test_step_started_显示步数():
    text = _render(StepStarted(envelope=EventEnvelope(step_index=2)))

    assert "Step 2" in text


def test_tool_call_started_显示名称与参数():
    text = _render(
        ToolCallStarted(
            tool_call=ToolCall(id="c1", name="echo", arguments={"text": "hi"}),
            batch_id="b1", call_index=0, total_calls=1,
        )
    )

    assert "echo" in text
    assert "running" in text


def test_tool_call_completed_成功显示_ok():
    text = _render(
        ToolCallCompleted(
            tool_call=ToolCall(id="c1", name="echo", arguments={}),
            tool_result=ToolExecutionResult(content="done"),
        )
    )

    assert "ok" in text
    assert "failed" not in text


def test_tool_call_completed_失败显示_failed():
    text = _render(
        ToolCallCompleted(
            tool_call=ToolCall(id="c1", name="missing", arguments={}),
            tool_result=ToolExecutionResult(content="not found", is_error=True),
        )
    )

    assert "failed" in text


def test_assistant_message_显示正文与用量_footer():
    text = _render(
        AssistantMessageEvent(
            text="hello world",
            usage=TurnUsage(
                steps=1, input_tokens=100, output_tokens=20,
                elapsed_ms=1500, model_label="anthropic / claude-jupiter-v1-p",
            ),
        )
    )

    assert "hello world" in text
    assert "anthropic / claude-jupiter-v1-p" in text
    assert "100" in text
    assert "20" in text


def test_assistant_message_无用量时不崩():
    text = _render(AssistantMessageEvent(text="hello"))

    assert "hello" in text


def test_turn_级事件不产生输出():
    """E1 阶段 turn_started/completed 只进 trace，不上屏。"""
    assert _render(TurnStarted(user_text="hi")).strip() == ""
    assert _render(TurnCompleted(usage=TurnUsage(steps=1))).strip() == ""
```

- [ ] **Step 2: 跑测试确认失败**

Run: `GEMINI_API_KEY=fake uv run --with pytest pytest tests/cli/test_event_rendering.py -q`
Expected: FAIL — `ImportError` 或 `AttributeError`（renderer 仍在按旧 `event_type` 分派）

- [ ] **Step 3: 改 `event_renderer.py`**

`handle_event` 改为按类型分派，`_render_assistant_footer` 改吃 `TurnUsage`：

```python
from rich.console import Console, Group, RenderableType
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text

from pickel.runs.runtime_events import (
    AssistantMessageEvent,
    RuntimeEventBase,
    StepStarted,
    ToolCallCompleted,
    ToolCallStarted,
)
from pickel.runs.turn_usage import TurnUsage
from pickel.tools.base import ToolExecutionResult


class ChatEventRenderer:
    def __init__(self, console: Console) -> None:
        self.console = console
        self.rendered_assistant_message = False

    async def handle_event(self, event: RuntimeEventBase) -> None:
        if isinstance(event, StepStarted):
            self._render_message(
                "Thinking",
                Text(f"Step {event.envelope.step_index}"),
                style="magenta",
            )
            return

        if isinstance(event, ToolCallStarted) and event.tool_call is not None:
            self._render_message(
                "Tool",
                self._render_tool_started(event.tool_call.name, event.tool_call.arguments),
                style="blue",
            )
            return

        if isinstance(event, ToolCallCompleted) and event.tool_call is not None:
            tool_result = event.tool_result or ToolExecutionResult(content="")
            self._render_message(
                "Tool",
                self._render_tool_finished(
                    event.tool_call.name, event.tool_call.arguments, tool_result
                ),
                style="red" if tool_result.is_error else "green",
            )
            return

        if isinstance(event, AssistantMessageEvent):
            self.rendered_assistant_message = True
            content: RenderableType = Markdown(event.text)
            if event.usage is not None:
                content = Group(
                    Markdown(event.text), self._render_assistant_footer(event.usage)
                )
            self._render_message("Assistant", content, style="yellow")

    def _render_assistant_footer(self, usage: TurnUsage) -> Text:
        footer = Text(style="dim", justify="right")
        if usage.model_label:
            footer.append(usage.model_label)
        stats = [
            f"in {usage.actual_input_tokens}",
            f"out {usage.output_tokens}",
        ]
        if usage.elapsed_ms:
            stats.append(f"{usage.elapsed_ms / 1000:.1f}s")
        footer.append("\n")
        footer.append(" · ".join(stats))
        return footer
```

`_render_tool_started` / `_render_tool_finished` / `_render_message` / `_format_tool_label` / `_truncate_content` 保持原样。

删除 `event_renderer.py:59-78` 的 `render_tool_batch_transcript` 类方法与 `ToolCallBatch` import——已确认零外部引用（只有定义处自身），且它依赖的 `ToolCallBatch` 是文件头注释里自认的「运行时/回放展示 shim」。删除后再确认一次：

```bash
grep -rn "render_tool_batch_transcript" src tests
```

Expected: 无输出。

- [ ] **Step 4: 改 `chat.py` 订阅 bus**

`chat.py:111-112` 的 `create_event_handler` 换成：

```python
    def create_event_bus(self) -> tuple[EventBus, ChatEventRenderer]:
        bus = EventBus()
        renderer = ChatEventRenderer(self.console)
        bus.subscribe(renderer.handle_event)
        if self._trace_sink is not None:
            bus.subscribe(self._trace_sink)
        return bus, renderer
```

`__init__` 末尾加 trace sink 构造：

```python
        self._trace_sink = None
        if trace_enabled(
            self._app_config.trace_enabled if self._app_config is not None else False
        ):
            self._trace_sink = JsonlTraceSink(trace_path(self.session.session_id))
```

`handle_user_input` 的 `event_handler` 参数换成 `bus`：

```python
    async def handle_user_input(
        self,
        text: str,
        bus: "EventBus | None" = None,
    ) -> AssistantMessage:
        if self._run is None:
            raise ValueError("Run 未提供")
        return await self._run.turn(
            session=self.session, user_text=text, bus=bus
        )
```

`chat.py:574-580` 的主循环改为：

```python
            bus, event_renderer = self.create_event_bus()
            start_index = len(self.session.entries)
            try:
                reply = await self.handle_user_input(user_input, bus=bus)
```

`_close_session` 里加 sink 关闭：

```python
        if self._trace_sink is not None:
            self._trace_sink.close()
            self._trace_sink = None
```

import 段加：

```python
from pickel.runs.event_bus import EventBus
from pickel.runs.trace_sink import JsonlTraceSink, trace_enabled, trace_path
```

- [ ] **Step 5: 修 `tests/cli/test_chat_loop.py`**

把其中所有 `event_handler=` 的用法改为 `bus=`，`create_event_handler()` 改为 `create_event_bus()`。先定位：

```bash
grep -n "event_handler\|create_event_handler" tests/cli/test_chat_loop.py
```

逐处按新签名改写。`/context` 相关的 4 个测试不受影响，不要动。

- [ ] **Step 6: 跑全量确认通过**

Run: `GEMINI_API_KEY=fake uv run --with pytest pytest -q`
Expected: 只剩基线那 6 个 `tests/tools/test_shell.py` 失败，其余全绿

- [ ] **Step 7: 提交**

```bash
git checkout uv.lock
git add -A
git commit -m "refactor(cli): 渲染器消费新事件类型，chat 订阅 EventBus"
```

---

## Task 8: 删旧事件模块 + 端到端验证

**Files:**
- Delete: `src/pickel/runs/events.py`
- Test: 手工端到端脚本（不进 repo）

**Interfaces:**
- Consumes: 全部前序任务
- Produces: 无

- [ ] **Step 1: 确认旧模块零引用**

```bash
grep -rn "runs.events\|RuntimeEventType\|from pickel.runs import RuntimeEvent\b" src tests
```

Expected: 无输出。有输出则先改干净，不要删。

- [ ] **Step 2: 删除并跑全量**

```bash
git rm src/pickel/runs/events.py
GEMINI_API_KEY=fake uv run --with pytest pytest -q
```

Expected: 只剩基线那 6 个 `tests/tools/test_shell.py` 失败

- [ ] **Step 3: 真实 provider 端到端**

fake provider 覆盖不到三件事：真实多 step 工具调用下的事件顺序、真实 usage 进 `TurnCompleted`、trace 在真实 `thought_signature` 下能否序列化。

写到 scratchpad（不进 repo）：

```python
# <scratchpad>/e1_events.py
"""E1 端到端：真实 provider 跑一轮带工具的对话，验证事件流与 trace。"""

import asyncio
import json
import os
from pathlib import Path

os.environ["PICKEL_TRACE"] = "1"

from pickel.app.boot import Boot
from pickel.config.loader import Config
from pickel.conversations.session import Session
from pickel.runs.event_bus import EventBus
from pickel.runs.runtime_events import (
    AssistantMessageEvent, StepStarted, ToolCallCompleted,
    ToolCallStarted, TurnCompleted, TurnStarted,
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
        user_text="用 shell_exec 跑 `echo hello-e1`，然后一句话告诉我输出。",
        bus=bus,
    )
    sink.close()

    print("=== 事件序列 ===")
    for event in events:
        print(f"  seq={event.envelope.seq:>2}  {type(event).__name__}")

    assert isinstance(events[0], TurnStarted), events[0]
    assert isinstance(events[-1], TurnCompleted), events[-1]
    assert [e.envelope.seq for e in events] == list(range(len(events)))
    assert len({e.envelope.turn_id for e in events}) == 1
    assert any(isinstance(e, ToolCallStarted) for e in events), "模型没调工具，换个提示重试"
    assert any(isinstance(e, ToolCallCompleted) for e in events)

    completed = events[-1]
    print(f"\n=== TurnCompleted ===")
    print(f"  steps={completed.usage.steps}  "
          f"actual_input={completed.usage.actual_input_tokens}  "
          f"out={completed.usage.output_tokens}  elapsed={completed.elapsed_ms}ms")
    assert completed.usage.steps >= 2, "带工具的一轮应有多次 generate"
    assert completed.usage.actual_input_tokens > 0

    lines = path.read_text(encoding="utf-8").strip().splitlines()
    print(f"\n=== trace: {path} ({len(lines)} 行) ===")
    assert len(lines) == len(events)
    for line in lines:
        json.loads(line)
    print(json.dumps(json.loads(lines[0]), ensure_ascii=False, indent=2)[:400])

    print("\n全部通过")


asyncio.run(main())
```

Run:
```bash
set -a && . ~/.pickel/.env && set +a && uv run python <scratchpad>/e1_events.py
```

Expected: 事件序列以 `TurnStarted` 开头、`TurnCompleted` 结尾，seq 连续，含至少一对 `ToolCallStarted`/`ToolCallCompleted`，trace 行数与事件数相等且每行是合法 JSON。

若模型没调工具，改提示词重试；若 `thought_signature` 相关的序列化报错，回 Task 2 修 `_tool_call_to_dict`。

- [ ] **Step 4: 交互确认 UI 无变化**

```bash
set -a && . ~/.pickel/.env && set +a && uv run pickel chat
```

问一句需要工具的问题，确认终端输出与改动前一致（Panel 布局、Thinking/Tool/Assistant 三种框、footer 格式）。E1 不改排版，任何肉眼可见的变化都是 bug。

退出后确认 `~/.pickel/traces/` 下**没有**新文件（默认关）。

- [ ] **Step 5: 提交**

```bash
git checkout uv.lock
git add -A
git commit -m "chore(events): 删除旧 RuntimeEvent 模块，E1 完成"
```

---

## 验收清单

- [ ] 全量测试：基线 6 个 shell 失败不增不减，新增测试全绿
- [ ] `grep -rn "RuntimeEventType" src tests` 无输出
- [ ] `grep -rn "rich" src/pickel/runs/` 无输出（红线 1）
- [ ] 订阅者抛异常时 turn 正常完成（`test_订阅者抛异常不传播给_emit_调用方`）
- [ ] 事件 seq 在真实一轮对话中连续无缺口
- [ ] trace 默认关：不设 `PICKEL_TRACE` 跑一轮，`~/.pickel/traces/` 无新文件
- [ ] trace 模块无任何读回接口（`test_模块不提供任何读回接口`）
- [ ] 交互式 CLI 输出与 E1 前一致

## 给 E2 的接口约定

E2 将新增 `ThinkingDelta` / `TextDelta` / `ToolCallDelta` / `TurnInterrupted`，全部继承 `RuntimeEventBase`，通过同一个 `EventBus` 发射。E1 的 `EventBus.emit` 每事件一次 `await`，delta 洪流下可能成为瓶颈——E2 若需批量或背压，改 `EventBus` 内部即可，事件类型与订阅者接口不变。
