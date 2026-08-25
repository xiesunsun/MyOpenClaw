"""JSONL trace：只写不读的派生轨迹（红线 5/6）。"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from pathlib import Path

import pickel.observe.jsonl_trace_sink as trace_module
from pickel.config.paths import home_dir
from pickel.observe.records import (
    DiagnosticRecord,
    RequestSnapshotRecord,
    SpanRecord,
)
from pickel.runtime.event_bus import EventBus
from pickel.runtime.runtime_events import (
    ModelStepStarted,
    TextDeltaEvent,
    ThinkingDeltaEvent,
    ToolCallArgsDeltaEvent,
    AgentRunInterrupted,
    AgentRunStarted,
)
from pickel.observe.jsonl_trace_sink import (
    JsonlTraceSink,
    TraceOptions,
    trace_enabled,
    trace_mode,
    trace_path,
)
from pickel.shared.event_envelope import EventEnvelope
from pickel.shared.execution_identity import ExecutionIdentity


def test_默认开启_standard():
    assert trace_mode() == "standard"
    assert trace_enabled() is True


def test_配置可开启():
    assert trace_enabled(True) is True


def test_环境变量覆盖配置(monkeypatch):
    monkeypatch.setenv("PICKEL_TRACE", "1")
    assert trace_enabled(False) is True


def test_环境变量为_0_时关闭即使配置为开(monkeypatch):
    monkeypatch.setenv("PICKEL_TRACE", "0")
    assert trace_enabled(True) is False


def test_app_config_默认_trace_standard():
    from pickel.config.loader import _BUILTIN_DEFAULTS

    assert _BUILTIN_DEFAULTS["observability"]["trace"]["mode"] == "standard"


def test_trace_path_是_home_下的_traces_目录里的_jsonl(tmp_path: Path, monkeypatch):
    """真实路径本身也要被断言：ChatLoop 的测试全都把 trace_path patch 掉了。"""
    monkeypatch.setenv("PICKEL_HOME", str(tmp_path))

    assert trace_path("s1") == tmp_path / "traces" / "s1.jsonl"
    assert trace_path("s1") == home_dir() / "traces" / "s1.jsonl"


def test_写出的每行都是合法_json(tmp_path: Path):
    path = tmp_path / "s1.jsonl"
    sink = JsonlTraceSink(path)
    bus = EventBus()
    bus.subscribe(sink)

    asyncio.run(_emit(bus))
    sink.close()

    lines = path.read_text(encoding="utf-8").strip().splitlines()
    records = [json.loads(line) for line in lines]
    assert len(records) == 2
    assert [record["event_type"] for record in records] == [
        "agent_run_started",
        "model_step_started",
    ]
    assert all(record["record_type"] == "runtime_event" for record in records)
    assert [record["trace_seq"] for record in records] == [0, 1]


def test_flush_等待已入队记录落盘且不关闭_sink(tmp_path: Path):
    path = tmp_path / "s1.jsonl"
    sink = JsonlTraceSink(path)
    bus = EventBus()
    bus.subscribe(sink)

    asyncio.run(_emit(bus))

    assert sink.flush() is True
    assert sink.closed is False
    assert len(path.read_text(encoding="utf-8").splitlines()) == 2
    sink.close()


async def _emit(bus: EventBus) -> None:
    await bus.emit(
        AgentRunStarted(
            envelope=EventEnvelope(identity=ExecutionIdentity(session_id="s1")),
            user_text="hi",
        )
    )
    await bus.emit(
        ModelStepStarted(
            envelope=EventEnvelope(
                identity=ExecutionIdentity(session_id="s1", step_sequence=1)
            )
        )
    )


def test_落盘的_seq_与_bus_分配一致(tmp_path: Path):
    path = tmp_path / "s1.jsonl"
    sink = JsonlTraceSink(path)
    bus = EventBus()
    bus.subscribe(sink)

    asyncio.run(_emit(bus))
    sink.close()

    seqs = [
        json.loads(line)["event_sequence"]
        for line in path.read_text().strip().splitlines()
    ]
    assert seqs == [0, 1]


def test_delta_事件写入_jsonl_后可_json_loads_读回(tmp_path: Path):
    """Task 4 新增的 4 个事件类型都必须能落盘成合法 JSON。"""
    path = tmp_path / "s1.jsonl"
    sink = JsonlTraceSink(path, TraceOptions(mode="full"))
    bus = EventBus()
    bus.subscribe(sink)

    asyncio.run(_emit_deltas(bus))
    sink.close()

    records = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").strip().splitlines()
    ]
    assert [record["event_type"] for record in records] == [
        "thinking_delta",
        "text_delta",
        "tool_call_args_delta",
        "agent_run_interrupted",
    ]
    assert records[0]["text"] == "想"
    assert records[1]["text"] == "你"
    assert records[2]["tool_call_id"] == "call-1"
    assert records[2]["partial_json"] == '{"text": "你'
    assert records[3]["at_step"] == 1
    assert records[3]["partial_text"] == "你"


def test_standard_不写逐块_delta(tmp_path: Path):
    path = tmp_path / "s1.jsonl"
    sink = JsonlTraceSink(path)
    bus = EventBus()
    bus.subscribe(sink)

    asyncio.run(_emit_deltas(bus))
    sink.close()

    records = [json.loads(line) for line in path.read_text().splitlines()]
    assert [record["event_type"] for record in records] == ["agent_run_interrupted"]


def _delta_event(call_id: str = "call-1") -> ToolCallArgsDeltaEvent:
    return ToolCallArgsDeltaEvent(
        envelope=EventEnvelope(
            identity=ExecutionIdentity(
                session_id="s1",
                operation_id="operation-1",
                step_id="step-1",
                step_sequence=1,
                tool_call_id=call_id,
            )
        ),
        partial_json="{}",
    )


def _replace_put(sink: JsonlTraceSink, put):
    sink._buffer.put = put


def test_delta_queue_full只报告首个丢帧且保留身份(tmp_path: Path):
    diagnostics = []
    sink = JsonlTraceSink(
        tmp_path / "s1.jsonl",
        TraceOptions(mode="full", queue_capacity=1),
        diagnostic_callback=diagnostics.append,
    )
    _replace_put(sink, lambda _item: (False, None))

    sink(_delta_event("call-1"))
    sink(_delta_event("call-2"))
    assert sink.flush() is True
    sink.close()

    assert len(diagnostics) == 1
    diagnostic = diagnostics[0]
    assert diagnostic.name == "trace_delta_dropped"
    assert diagnostic.level == "warning"
    assert diagnostic.identity.tool_call_id == "call-1"
    assert diagnostic.occurred_at.tzinfo is not None
    assert diagnostic.attributes == {
        "reason": "queue_full",
        "event_type": "tool_call_args_delta",
        "records_dropped": 1,
        "delta_records_dropped": 1,
        "queue_capacity": 1,
    }


def test_高优先级入队报告被淘汰_delta(tmp_path: Path):
    diagnostics = []
    sink = JsonlTraceSink(
        tmp_path / "s1.jsonl",
        TraceOptions(mode="full", queue_capacity=1),
        diagnostic_callback=diagnostics.append,
    )
    delta = _delta_event("call-1")
    evicted = trace_module._QueuedRecord(
        value={},
        low_priority=True,
        identity=delta.envelope.identity,
        event_type="tool_call_args_delta",
    )
    calls = []

    def put(item):
        calls.append(item)
        return (True, evicted) if not item.low_priority else (True, None)

    _replace_put(sink, put)
    sink(delta)
    sink.record(DiagnosticRecord(name="important"))
    assert sink.flush() is True
    sink.close()

    assert len(diagnostics) == 1
    assert diagnostics[0].attributes["reason"] == "evicted_delta"
    assert diagnostics[0].identity.tool_call_id == "call-1"
    assert calls[-1].low_priority is False


def test_delta成功入队后开启新的拥塞周期(tmp_path: Path):
    diagnostics = []
    sink = JsonlTraceSink(
        tmp_path / "s1.jsonl",
        TraceOptions(mode="full", queue_capacity=1),
        diagnostic_callback=diagnostics.append,
    )
    reject = lambda _item: (False, None)
    accept = lambda item: (True, None)
    _replace_put(sink, reject)
    sink(_delta_event("call-1"))
    assert sink.flush() is True
    _replace_put(sink, accept)
    sink(_delta_event("call-2"))
    _replace_put(sink, reject)
    sink(_delta_event("call-3"))
    assert sink.flush() is True
    sink.close()

    assert [item.identity.tool_call_id for item in diagnostics] == [
        "call-1",
        "call-3",
    ]


def test_pending_diagnostic未发出时成功_delta不覆盖首条身份(tmp_path: Path):
    diagnostics = []
    sink = JsonlTraceSink(
        tmp_path / "s1.jsonl",
        TraceOptions(mode="full", queue_capacity=1),
        diagnostic_callback=diagnostics.append,
    )
    entered = threading.Event()
    release = threading.Event()
    original_emit = sink._emit_pending_diagnostic

    def blocked_emit():
        entered.set()
        assert release.wait(1)
        original_emit()

    sink._emit_pending_diagnostic = blocked_emit
    low_calls = 0

    def put(item):
        nonlocal low_calls
        if not item.low_priority:
            return True, None
        low_calls += 1
        return (False, None) if low_calls in {1, 3} else (True, None)

    _replace_put(sink, put)
    sink(_delta_event("call-1"))
    assert entered.wait(1)
    sink(_delta_event("call-2"))
    sink(_delta_event("call-3"))

    with sink._diagnostic_lock:
        assert sink._pending_diagnostic is not None
        assert sink._pending_diagnostic.identity.tool_call_id == "call-1"

    release.set()
    assert sink.flush() is True
    sink.close()

    assert [item.identity.tool_call_id for item in diagnostics] == ["call-1"]


def test_diagnostic_callback重入_sink不会递归(tmp_path: Path):
    diagnostics = []
    callback_started = threading.Event()

    def callback(diagnostic):
        diagnostics.append(diagnostic)
        callback_started.set()
        sink.record(DiagnosticRecord(name="callback_record"))

    sink = JsonlTraceSink(
        tmp_path / "s1.jsonl",
        TraceOptions(mode="full", queue_capacity=1),
        diagnostic_callback=callback,
    )
    _replace_put(sink, lambda _item: (False, None))
    sink(_delta_event())
    assert callback_started.wait(1)
    sink.close()

    assert len(diagnostics) == 1


def test_close也会发出pending_diagnostic且只发一次(tmp_path: Path):
    diagnostics = []
    sink = JsonlTraceSink(
        tmp_path / "s1.jsonl",
        TraceOptions(mode="full", queue_capacity=1),
        diagnostic_callback=diagnostics.append,
    )
    _replace_put(sink, lambda _item: (False, None))
    sink(_delta_event())
    sink.close()
    sink.close()

    assert len(diagnostics) == 1


def test_无callback时由writer记录一次warning(tmp_path: Path, caplog):
    sink = JsonlTraceSink(
        tmp_path / "s1.jsonl",
        TraceOptions(mode="full", queue_capacity=1),
    )
    _replace_put(sink, lambda _item: (False, None))
    with caplog.at_level(logging.WARNING):
        sink(_delta_event())
        assert sink.flush() is True
    sink.close()

    assert [
        record for record in caplog.records if "trace delta dropped" in record.message
    ]


def test_slow_diagnostic_callback不阻塞_enqueue(tmp_path: Path):
    callback_started = threading.Event()
    callback_release = threading.Event()

    def callback(_diagnostic):
        callback_started.set()
        callback_release.wait(1)

    sink = JsonlTraceSink(
        tmp_path / "s1.jsonl",
        TraceOptions(mode="full", queue_capacity=1),
        diagnostic_callback=callback,
    )
    _replace_put(sink, lambda _item: (False, None))
    started = time.perf_counter()
    sink(_delta_event())
    elapsed = time.perf_counter() - started
    assert elapsed < 0.2
    assert callback_started.wait(1)
    callback_release.set()
    sink.close()


def test_observer_span_与_runtime_event_共用_trace_seq(tmp_path: Path):
    path = tmp_path / "s1.jsonl"
    sink = JsonlTraceSink(path)
    sink(ModelStepStarted())
    sink.record(
        SpanRecord(
            name="pickel.provider.request",
            identity=ExecutionIdentity(session_id="s1", operation_id="t1"),
            duration_ms=12.5,
        )
    )
    sink.close()

    records = [json.loads(line) for line in path.read_text().splitlines()]
    assert [record["record_type"] for record in records] == [
        "runtime_event",
        "span",
    ]
    assert [record["trace_seq"] for record in records] == [0, 1]


def test_observation_identity_沿用旧的空_operation_id输出(tmp_path: Path):
    path = tmp_path / "s1.jsonl"
    sink = JsonlTraceSink(path)
    sink.record(DiagnosticRecord(name="diagnostic"))
    sink.close()

    record = json.loads(path.read_text())
    assert record["operation_id"] == ""
    assert "tool_call_id" not in record
    assert "message_id" not in record


def test_完整请求快照只在_full_模式落盘(tmp_path: Path):
    snapshot = RequestSnapshotRecord(
        identity=ExecutionIdentity(session_id="s1", operation_id="t1", step_sequence=1),
        provider="anthropic",
        model="claude-test",
        cache_order=("tools", "system", "messages"),
        request={"system": "SECRET", "messages": []},
    )
    standard_path = tmp_path / "standard.jsonl"
    standard = JsonlTraceSink(standard_path)
    assert standard.wants("request_snapshot") is False
    standard.record(snapshot)
    standard.close()
    assert standard_path.read_text() == ""

    full_path = tmp_path / "full.jsonl"
    full = JsonlTraceSink(full_path, TraceOptions(mode="full"))
    assert full.wants("request_snapshot") is True
    full.record(snapshot)
    full.close()

    record = json.loads(full_path.read_text())
    assert record["record_type"] == "request_snapshot"
    assert record["payload"]["cache_order"] == ["tools", "system", "messages"]
    assert record["payload"]["request"]["system"] == "SECRET"


def test_超过文件上限后轮转(tmp_path: Path):
    path = tmp_path / "s1.jsonl"
    sink = JsonlTraceSink(
        path,
        TraceOptions(mode="full", batch_size=1, max_file_size_mb=1),
    )
    sink(TextDeltaEvent(text="x" * 1_100_000))
    sink(TextDeltaEvent(text="y" * 10))
    sink.close()

    rotated = list(tmp_path.glob("s1.*.jsonl"))
    assert len(rotated) == 1
    assert path.exists()


async def _emit_deltas(bus: EventBus) -> None:
    envelope = EventEnvelope(
        identity=ExecutionIdentity(session_id="s1", step_sequence=1)
    )
    await bus.emit(ThinkingDeltaEvent(envelope=envelope, text="想"))
    await bus.emit(TextDeltaEvent(envelope=envelope, text="你"))
    await bus.emit(
        ToolCallArgsDeltaEvent(
            envelope=EventEnvelope(
                identity=ExecutionIdentity(
                    session_id="s1", step_sequence=1, tool_call_id="call-1"
                )
            ),
            partial_json='{"text": "你',
        )
    )
    await bus.emit(AgentRunInterrupted(envelope=envelope, at_step=1, partial_text="你"))


def test_父目录不存在时自动创建(tmp_path: Path):
    path = tmp_path / "nested" / "deep" / "s1.jsonl"
    sink = JsonlTraceSink(path)
    sink(ModelStepStarted())
    sink.close()

    assert path.is_file()


def test_close_后不再写入(tmp_path: Path):
    path = tmp_path / "s1.jsonl"
    sink = JsonlTraceSink(path)
    sink(ModelStepStarted())
    sink.close()

    before = path.read_text(encoding="utf-8")
    try:
        sink(ModelStepStarted())
    except ValueError:
        pass  # 写已关闭的文件句柄

    assert path.read_text(encoding="utf-8") == before


def test_模块不提供任何读回接口():
    """红线 5：trace 是派生物，禁止从中重建对话或用量。"""
    source = Path(trace_module.__file__).read_text(encoding="utf-8")

    assert "def load" not in source
    assert "def replay" not in source
    assert "json.loads" not in source
