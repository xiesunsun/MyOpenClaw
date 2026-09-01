"""JSONL trace：只写不读的派生轨迹（红线 5/6）。"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

import pickel.observe.jsonl_trace_sink as trace_module
from pickel.config.paths import home_dir
from pickel.telemetry.records import (
    DiagnosticRecord,
    SpanRecord,
    observation_scope,
)
from pickel.runtime.event_bus import EventBus
from pickel.runtime.runtime_events import (
    AssistantMessageEvent,
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
        "assistant_message",
    ]
    assert all(record["record_type"] == "runtime_event" for record in records)
    assert all(record["mode"] == "standard" for record in records)
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


def test_reopen_continues_file_trace_sequence(tmp_path: Path):
    path = tmp_path / "s1.jsonl"
    first = JsonlTraceSink(path)
    first_bus = EventBus()
    first_bus.subscribe(first)
    asyncio.run(_emit(first_bus))
    first.close()

    second = JsonlTraceSink(path)
    second_bus = EventBus()
    second_bus.subscribe(second)
    asyncio.run(_emit(second_bus))
    second.close()

    records = [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
    ]
    assert [record["trace_seq"] for record in records] == [0, 1, 2, 3]


def test_sequence只读取各段尾部并忽略崩溃半行(tmp_path: Path):
    path = tmp_path / "s1.jsonl"
    rotated = tmp_path / "s1.20260101T000000.0001.jsonl"
    rotated.write_text(
        ("x" * 200_000) + "\n" + json.dumps({"trace_seq": 41}) + "\n",
        encoding="utf-8",
    )
    path.write_text(
        json.dumps({"trace_seq": 42}) + "\n" + '{"trace_seq":999',
        encoding="utf-8",
    )
    assert rotated.stat().st_size > trace_module._TRACE_SEQUENCE_TAIL_BYTES

    sink = JsonlTraceSink(path)
    sink(AssistantMessageEvent())
    sink.close()

    records = [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
    ]
    # The half-written record's allocated sequence is reserved, even though
    # its bytes are truncated before the next append.
    assert records[-1]["trace_seq"] == 1000


def test_sequence跨rotation继续递增且不覆盖旧段(tmp_path: Path):
    path = tmp_path / "s1.jsonl"
    old = tmp_path / "s1.20260101T000000.0001.jsonl"
    old.write_text(json.dumps({"trace_seq": 77}) + "\n", encoding="utf-8")
    path.write_text(json.dumps({"trace_seq": 78}) + "\n", encoding="utf-8")

    sink = JsonlTraceSink(path, TraceOptions(max_file_size_mb=1))
    sink(AssistantMessageEvent(text="new"))
    sink.close()

    records = [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
    ]
    assert records[-1]["trace_seq"] == 79
    assert old.exists()


def test_retention限制整个trace根目录且不删除当前写入文件(tmp_path: Path):
    first = tmp_path / "a.jsonl"
    second = tmp_path / "b.jsonl"
    current = tmp_path / "current.jsonl"
    first.write_bytes(b"a" * 600_000 + b"\n")
    second.write_bytes(b"b" * 600_000 + b"\n")
    old_time = time.time() - 10
    os.utime(first, (old_time, old_time))
    os.utime(second, (old_time, old_time))

    sink = JsonlTraceSink(
        current,
        TraceOptions(max_total_size_mb=1, max_age_days=0),
    )
    sink.close()

    assert current.exists()
    assert not first.exists()
    assert second.exists()


def test_retention不会删除同进程另一个正在写入的文件(tmp_path: Path):
    other = tmp_path / "other.jsonl"
    old = tmp_path / "old.jsonl"
    current = tmp_path / "current.jsonl"
    other.write_bytes(b"o" * 700_000 + b"\n")
    old.write_bytes(b"x" * 700_000 + b"\n")
    timestamp = time.time() - 10
    os.utime(other, (timestamp, timestamp))
    os.utime(old, (timestamp - 1, timestamp - 1))

    other_sink = JsonlTraceSink(other, TraceOptions(max_total_size_mb=1))
    sink = JsonlTraceSink(
        current,
        TraceOptions(max_total_size_mb=1, max_age_days=0),
    )
    sink.close()
    other_sink.close()

    assert other.exists()
    assert not old.exists()


async def _emit(bus: EventBus) -> None:
    await bus.emit(
        AgentRunStarted(
            envelope=EventEnvelope(identity=ExecutionIdentity(session_id="s1")),
            user_text="hi",
        )
    )
    await bus.emit(
        AssistantMessageEvent(
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


def test_full_将逐块_delta汇总为单条记录(tmp_path: Path):
    path = tmp_path / "s1.jsonl"
    sink = JsonlTraceSink(path, TraceOptions(mode="full"))
    bus = EventBus()
    bus.subscribe(sink)

    with observation_scope(sink):
        asyncio.run(_emit_deltas(bus))
    sink.close()

    records = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").strip().splitlines()
    ]
    assert [record["record_type"] for record in records] == [
        "stream_delta_summary",
        "runtime_event",
        "span",
    ]
    summary = records[0]["payload"]
    assert summary["delta_count"] == 3
    assert summary["thinking"] == {"count": 1, "chars": 1, "utf8_bytes": 3}
    assert summary["text"] == {"count": 1, "chars": 1, "utf8_bytes": 3}
    assert summary["tool_call_args"] == {
        "count": 1,
        "chars": 11,
        "utf8_bytes": 13,
    }
    assert records[1]["event_type"] == "agent_run_interrupted"
    assert records[2]["payload"]["name"] == "pickel.event.delivery"


def test_standard_不写逐块_delta(tmp_path: Path):
    path = tmp_path / "s1.jsonl"
    sink = JsonlTraceSink(path)
    bus = EventBus()
    bus.subscribe(sink)

    with observation_scope(sink):
        asyncio.run(_emit_deltas(bus))
    sink.close()

    records = [json.loads(line) for line in path.read_text().splitlines()]
    assert [record["record_type"] for record in records] == [
        "runtime_event",
        "span",
    ]
    assert records[0]["event_type"] == "agent_run_interrupted"
    assert records[1]["payload"]["name"] == "pickel.event.delivery"


def test_full_一万条delta仍保持常数级记录(tmp_path: Path):
    path = tmp_path / "s1.jsonl"
    sink = JsonlTraceSink(path, TraceOptions(mode="full"))
    bus = EventBus()
    bus.subscribe(sink)
    envelope = EventEnvelope(
        identity=ExecutionIdentity(
            session_id="s1",
            operation_id="operation-1",
            step_id="step-1",
            step_sequence=1,
            model_call_id="model-call-1",
        )
    )

    async def emit_many() -> None:
        for _ in range(10_000):
            await bus.emit(TextDeltaEvent(envelope=envelope, text="字"))

    with observation_scope(sink):
        asyncio.run(emit_many())
    sink.close()

    records = [json.loads(line) for line in path.read_text().splitlines()]
    assert len(records) == 1
    assert records[0]["record_type"] == "stream_delta_summary"
    assert records[0]["payload"]["delta_count"] == 10_000
    assert records[0]["payload"]["text"] == {
        "count": 10_000,
        "chars": 10_000,
        "utf8_bytes": 30_000,
    }
    assert path.stat().st_size < 2_000


def test_observer_span_与_runtime_event_共用_trace_seq(tmp_path: Path):
    path = tmp_path / "s1.jsonl"
    sink = JsonlTraceSink(path)
    sink(AssistantMessageEvent())
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


def test_span_trace保留_tool_call_执行身份(tmp_path: Path):
    path = tmp_path / "s1.jsonl"
    sink = JsonlTraceSink(path)
    sink.record(
        SpanRecord(
            name="pickel.tool.execute",
            identity=ExecutionIdentity(
                session_id="s1", operation_id="op-1", tool_call_id="call-1"
            ),
        )
    )
    sink.close()

    record = json.loads(path.read_text())
    assert record["tool_call_id"] == "call-1"


def test_observation_identity_沿用旧的空_operation_id输出(tmp_path: Path):
    path = tmp_path / "s1.jsonl"
    sink = JsonlTraceSink(path)
    sink.record(DiagnosticRecord(name="diagnostic"))
    sink.close()

    record = json.loads(path.read_text())
    assert record["operation_id"] == ""
    assert "tool_call_id" not in record
    assert "message_id" not in record


def test_超过文件上限后轮转(tmp_path: Path):
    path = tmp_path / "s1.jsonl"
    sink = JsonlTraceSink(
        path,
        TraceOptions(mode="full", batch_size=1, max_file_size_mb=1),
    )
    sink(AssistantMessageEvent(text="x" * 1_100_000))
    sink(AssistantMessageEvent(text="y" * 10))
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
    sink(AssistantMessageEvent())
    sink.close()

    assert path.is_file()


def test_close_后不再写入(tmp_path: Path):
    path = tmp_path / "s1.jsonl"
    sink = JsonlTraceSink(path)
    sink(AssistantMessageEvent())
    sink.close()

    before = path.read_text(encoding="utf-8")
    try:
        sink(AssistantMessageEvent())
    except ValueError:
        pass  # 写已关闭的文件句柄

    assert path.read_text(encoding="utf-8") == before


def test_模块不提供任何读回接口():
    """红线 5：trace 是派生物，禁止从中重建对话或用量。"""
    source = Path(trace_module.__file__).read_text(encoding="utf-8")

    assert "def load" not in source
    assert "def replay" not in source
    assert "json.loads" not in source
