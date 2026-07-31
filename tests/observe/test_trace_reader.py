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
            "tool_call": {
                "id": "c1",
                "name": "shell_exec",
                "arguments": {"cmd": "SECRET2"},
            },
        },
        {
            "event_type": "tool_call_completed",
            "seq": 2,
            "occurred_at": "2026-07-26T00:00:02.500000+00:00",
            "tool_call": {
                "id": "c1",
                "name": "shell_exec",
                "arguments": {"cmd": "SECRET2"},
            },
            "tool_result": {
                "content": [{"type": "text", "text": "SECRET3"}],
                "is_error": False,
            },
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
        {
            "event_type": "turn_started",
            "seq": 0,
            "occurred_at": "2026-07-26T00:00:00+00:00",
        },
        {
            "event_type": "turn_interrupted",
            "seq": 1,
            "occurred_at": "2026-07-26T00:00:01+00:00",
            "at_step": 2,
        },
    ]
    trace_file.write_text(
        "\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8"
    )

    enhancement = read_trace(trace_file)

    assert enhancement.turn_markers[0].interrupted is True


def test_request_digests_grouped_by_turn(tmp_path):
    trace_file = tmp_path / "s.jsonl"
    events = [
        {
            "event_type": "turn_started",
            "seq": 0,
            "occurred_at": "2026-07-27T00:00:00+00:00",
            "user_text": "SECRET",
        },
        {
            "event_type": "request_digest",
            "seq": 1,
            "occurred_at": "2026-07-27T00:00:01+00:00",
            "system_sections": [{"name": "behavior", "chars": 120}],
            "tool_names": ["shell_exec"],
            "message_count": 3,
            "request_chars": 4500,
            "hook_injected_chars": 0,
        },
        {
            "event_type": "request_digest",
            "seq": 2,
            "occurred_at": "2026-07-27T00:00:02+00:00",
            "system_sections": [{"name": "behavior", "chars": 120}],
            "tool_names": ["shell_exec"],
            "message_count": 5,
            "request_chars": 5200,
            "hook_injected_chars": 40,
        },
        {
            "event_type": "turn_started",
            "seq": 3,
            "occurred_at": "2026-07-27T00:01:00+00:00",
        },
        {
            "event_type": "request_digest",
            "seq": 4,
            "occurred_at": "2026-07-27T00:01:01+00:00",
            "system_sections": [],
            "tool_names": [],
            "message_count": 7,
            "request_chars": 6000,
            "hook_injected_chars": 0,
        },
    ]
    trace_file.write_text(
        "\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8"
    )

    enhancement = read_trace(trace_file)

    assert len(enhancement.request_digests) == 2
    first, second = enhancement.request_digests
    assert len(first) == 2
    assert first[0]["message_count"] == 3
    assert first[1]["request_chars"] == 5200
    assert first[1]["hook_injected_chars"] == 40
    assert first[0]["tool_names"] == ["shell_exec"]
    assert first[0]["system_sections"] == [{"name": "behavior", "chars": 120}]
    assert len(second) == 1


def test_request_digest_before_any_turn_is_dropped(tmp_path):
    trace_file = tmp_path / "s.jsonl"
    events = [
        {
            "event_type": "request_digest",
            "seq": 0,
            "occurred_at": "2026-07-27T00:00:00+00:00",
            "system_sections": [],
            "tool_names": [],
            "message_count": 1,
            "request_chars": 100,
            "hook_injected_chars": 0,
        },
    ]
    trace_file.write_text(json.dumps(events[0]) + "\n", encoding="utf-8")

    enhancement = read_trace(trace_file)

    assert enhancement.request_digests == []


def test_full_request_snapshots_grouped_by_turn(tmp_path):
    trace_file = tmp_path / "s.jsonl"
    events = [
        {
            "record_type": "runtime_event",
            "event_type": "turn_started",
            "turn_id": "t1",
            "occurred_at": "2026-07-31T00:00:00+00:00",
        },
        {
            "record_type": "request_snapshot",
            "turn_id": "t1",
            "step_index": 1,
            "payload": {
                "provider": "anthropic",
                "model": "claude-test",
                "cache_order": ["tools", "system", "messages"],
                "request": {
                    "system": "FULL SYSTEM",
                    "messages": [{"role": "user", "content": "FULL USER"}],
                    "tools": [{"name": "echo"}],
                },
            },
        },
    ]
    trace_file.write_text(
        "\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8"
    )

    enhancement = read_trace(trace_file)

    snapshot = enhancement.request_snapshots[0][0]
    assert snapshot["cache_order"] == ["tools", "system", "messages"]
    assert snapshot["request"]["system"] == "FULL SYSTEM"
    assert snapshot["request"]["messages"][0]["content"] == "FULL USER"


def test_span_metrics_include_percentiles_success_and_tokens(tmp_path):
    trace_file = tmp_path / "s.jsonl"
    spans = []
    for index, duration in enumerate([100, 200, 1000]):
        spans.append(
            {
                "record_type": "span",
                "payload": {
                    "name": "pickel.provider.request",
                    "duration_ms": duration,
                    "status": "error" if index == 2 else "ok",
                    "attributes": {
                        "ttft_ms": duration / 2,
                        "input_tokens": 10,
                        "output_tokens": 2,
                        "cache_read_tokens": 5,
                        "cache_write_tokens": 1,
                    },
                },
            }
        )
    trace_file.write_text(
        "\n".join(json.dumps(span) for span in spans) + "\n", encoding="utf-8"
    )

    enhancement = read_trace(trace_file)
    provider = enhancement.metrics["provider"]

    assert provider["count"] == 3
    assert provider["success_count"] == 2
    assert provider["success_rate"] == 0.6667
    assert provider["duration_ms"] == {"p50": 200.0, "p95": 1000.0, "p99": 1000.0}
    assert provider["ttft_ms"]["p50"] == 100.0
    assert provider["tokens"]["input_tokens"] == 30


def test_rotated_segments_are_read_before_active_file(tmp_path):
    active = tmp_path / "s.jsonl"
    rotated = tmp_path / "s.20260731T000000.0001.jsonl"
    rotated.write_text(
        json.dumps(
            {
                "event_type": "turn_started",
                "occurred_at": "2026-07-31T00:00:00+00:00",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    active.write_text(
        json.dumps(
            {
                "event_type": "turn_failed",
                "occurred_at": "2026-07-31T00:00:01+00:00",
                "error_type": "RuntimeError",
                "message": "boom",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    enhancement = read_trace(active)

    assert enhancement.turn_markers[0].failed["error_type"] == "RuntimeError"
