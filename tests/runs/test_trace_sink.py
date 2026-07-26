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
