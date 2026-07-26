"""trace 白名单读取:只取时序与终态,不取对话与用量(红线 5)。"""

import json
from dataclasses import asdict
from pathlib import Path

from pickel.observe.trace_reader import read_trace


def _write_trace(path: Path) -> None:
    events = [
        {
            "event_type": "turn_started",
            "seq": 0,
            "occurred_at": "2026-07-26T00:00:00+00:00",
            "turn_id": "t1",
            "user_text": "SECRET",
        },
        {
            "event_type": "tool_call_started",
            "seq": 1,
            "occurred_at": "2026-07-26T00:00:01+00:00",
            "tool_call": {"id": "c1", "name": "shell_exec", "arguments": {"cmd": "SECRET2"}},
        },
        {
            "event_type": "tool_call_completed",
            "seq": 2,
            "occurred_at": "2026-07-26T00:00:02.500000+00:00",
            "tool_call": {"id": "c1", "name": "shell_exec", "arguments": {"cmd": "SECRET2"}},
            "tool_result": {"content": [{"type": "text", "text": "SECRET3"}], "is_error": False},
        },
        {
            "event_type": "turn_failed",
            "seq": 3,
            "occurred_at": "2026-07-26T00:00:03+00:00",
            "error_type": "RuntimeError",
            "message": "boom",
            "traceback": "Traceback ...",
        },
    ]
    lines = [json.dumps(event) for event in events]
    lines.insert(2, "{broken json")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_read_trace_timings_and_markers(tmp_path):
    trace_file = tmp_path / "s.jsonl"
    _write_trace(trace_file)

    enhancement = read_trace(trace_file)

    assert enhancement is not None
    timing = enhancement.tool_timings["c1"]
    assert timing.started_at == "2026-07-26T00:00:01+00:00"
    assert timing.completed_at == "2026-07-26T00:00:02.500000+00:00"
    assert timing.duration_ms == 1500
    assert len(enhancement.turn_markers) == 1
    marker = enhancement.turn_markers[0]
    assert marker.started_at == "2026-07-26T00:00:00+00:00"
    assert marker.failed == {"error_type": "RuntimeError", "message": "boom"}
    assert marker.interrupted is False


def test_whitelist_excludes_conversation_content(tmp_path):
    trace_file = tmp_path / "s.jsonl"
    _write_trace(trace_file)

    enhancement = read_trace(trace_file)

    serialized = json.dumps(asdict(enhancement))
    assert "SECRET" not in serialized


def test_missing_file_returns_none(tmp_path):
    assert read_trace(tmp_path / "absent.jsonl") is None


def test_interrupted_marks_turn(tmp_path):
    trace_file = tmp_path / "s.jsonl"
    events = [
        {"event_type": "turn_started", "seq": 0, "occurred_at": "2026-07-26T00:00:00+00:00"},
        {"event_type": "turn_interrupted", "seq": 1, "occurred_at": "2026-07-26T00:00:01+00:00", "at_step": 2},
    ]
    trace_file.write_text(
        "\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8"
    )

    enhancement = read_trace(trace_file)

    assert enhancement.turn_markers[0].interrupted is True
