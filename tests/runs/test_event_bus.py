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
