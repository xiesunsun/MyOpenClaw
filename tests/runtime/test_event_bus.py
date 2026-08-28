"""EventBus：seq 全序分配 + 订阅者异常隔离。"""

from __future__ import annotations

import asyncio

from pickel.telemetry.records import SpanRecord, observation_scope
from pickel.runtime.event_bus import EventBus
from pickel.runtime.runtime_events import AssistantMessageEvent, AgentRunStarted
from pickel.shared.event_envelope import EventEnvelope
from pickel.shared.execution_identity import ExecutionIdentity


def test_seq_从_0_起严格递增():
    bus = EventBus()
    seen = []
    bus.subscribe(lambda event: seen.append(event.envelope.event_sequence))

    asyncio.run(_emit_many(bus, 3))

    assert seen == [0, 1, 2]


async def _emit_many(bus: EventBus, count: int) -> None:
    for _ in range(count):
        await bus.emit(
            AssistantMessageEvent(
                envelope=EventEnvelope(identity=ExecutionIdentity(session_id="s1"))
            )
        )


def test_emit_返回带_seq_的事件且不改原件():
    bus = EventBus()
    original = AssistantMessageEvent(
        envelope=EventEnvelope(identity=ExecutionIdentity(session_id="s1"))
    )

    emitted = asyncio.run(bus.emit(original))

    assert emitted.envelope.event_sequence == 0
    assert original.envelope.event_sequence == -1


def test_多订阅者都收到同一事件():
    bus = EventBus()
    a, b = [], []
    bus.subscribe(lambda e: a.append(e))
    bus.subscribe(lambda e: b.append(e))

    asyncio.run(bus.emit(AgentRunStarted(user_text="hi")))

    assert len(a) == 1 and len(b) == 1
    assert a[0].envelope.event_sequence == b[0].envelope.event_sequence


def test_订阅者抛异常不影响其余订阅者():
    bus = EventBus()
    survivors = []

    def explode(event):
        raise RuntimeError("renderer crashed")

    bus.subscribe(explode)
    bus.subscribe(lambda e: survivors.append(e))

    asyncio.run(bus.emit(AssistantMessageEvent()))

    assert len(survivors) == 1


def test_唯一订阅者抛异常时_emit_仍正常返回():
    """红线 2：UI 崩了不该杀掉正在跑的 turn。"""
    bus = EventBus()

    def explode(event):
        raise RuntimeError("boom")

    bus.subscribe(explode)

    emitted = asyncio.run(bus.emit(AssistantMessageEvent()))

    assert emitted.envelope.event_sequence == 0


def test_emit_records_event_delivery_span():
    bus = EventBus()
    records: list[SpanRecord] = []

    class Collector:
        def record(self, record) -> None:
            if isinstance(record, SpanRecord):
                records.append(record)

    with observation_scope(Collector()):
        asyncio.run(bus.emit(AssistantMessageEvent()))

    assert len(records) == 1
    assert records[0].name == "pickel.event.delivery"
    assert records[0].status == "ok"


def test_异步订阅者被_await():
    bus = EventBus()
    seen = []

    async def handler(event):
        await asyncio.sleep(0)
        seen.append(event)

    bus.subscribe(handler)
    asyncio.run(bus.emit(AssistantMessageEvent()))

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
    asyncio.run(bus.emit(AssistantMessageEvent()))

    assert len(survivors) == 1


def test_退订后不再收到事件():
    bus = EventBus()
    seen = []
    unsubscribe = bus.subscribe(lambda e: seen.append(e))

    asyncio.run(bus.emit(AssistantMessageEvent()))
    unsubscribe()
    asyncio.run(bus.emit(AssistantMessageEvent()))

    assert len(seen) == 1


def test_无订阅者时_emit_仍分配_seq():
    bus = EventBus()

    first = asyncio.run(bus.emit(AssistantMessageEvent()))
    second = asyncio.run(bus.emit(AssistantMessageEvent()))

    assert (
        first.envelope.event_sequence,
        second.envelope.event_sequence,
    ) == (0, 1)


def test_退订同一绑定方法的其中一次订阅不影响顺序与另一次():
    """绑定方法两次取值 == 成立；退订必须按订阅时的身份（token），

    而非按 == 匹配删除，否则会删错槽位、打乱剩余订阅者的调用顺序。
    """
    bus = EventBus()
    call_order: list[str] = []

    class Renderer:
        def handle(self, event) -> None:
            call_order.append("renderer")

    renderer = Renderer()

    def other(event) -> None:
        call_order.append("other")

    bus.subscribe(renderer.handle)
    bus.subscribe(other)
    unsubscribe_second_renderer_sub = bus.subscribe(renderer.handle)

    unsubscribe_second_renderer_sub()
    asyncio.run(bus.emit(AssistantMessageEvent()))

    assert call_order == ["renderer", "other"]
