"""trace 白名单读取:只取时序与终态,不取对话与用量(红线 5)。"""

import json
from dataclasses import asdict
from pathlib import Path

from pickel.observe.trace_reader import read_operation_trace, read_trace


def _write_trace(path: Path) -> None:
    events = [
        {
            "event_type": "agent_run_started",
            "event_sequence": 0,
            "occurred_at": "2026-07-26T00:00:00+00:00",
            "operation_id": "t1",
            "user_text": "SECRET",
        },
        {
            "event_type": "agent_run_failed",
            "event_sequence": 1,
            "occurred_at": "2026-07-26T00:00:03+00:00",
            "error_type": "RuntimeError",
            "message": "boom",
            "traceback": "Traceback ...",
        },
    ]
    lines = [json.dumps(event) for event in events]
    lines.insert(2, "{broken json")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_read_trace_markers(tmp_path):
    trace_file = tmp_path / "s.jsonl"
    _write_trace(trace_file)

    enhancement = read_trace(trace_file)

    assert enhancement is not None
    assert len(enhancement.agent_run_markers) == 1
    marker = enhancement.agent_run_markers[0]
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


def test_interrupted_marks_agent_run(tmp_path):
    trace_file = tmp_path / "s.jsonl"
    events = [
        {
            "event_type": "agent_run_started",
            "event_sequence": 0,
            "occurred_at": "2026-07-26T00:00:00+00:00",
        },
        {
            "event_type": "agent_run_interrupted",
            "event_sequence": 1,
            "occurred_at": "2026-07-26T00:00:01+00:00",
            "at_step": 2,
        },
    ]
    trace_file.write_text(
        "\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8"
    )

    enhancement = read_trace(trace_file)

    assert enhancement.agent_run_markers[0].interrupted is True


def test_full_request_snapshots_grouped_by_agent_run(tmp_path):
    trace_file = tmp_path / "s.jsonl"
    events = [
        {
            "record_type": "runtime_event",
            "event_type": "agent_run_started",
            "operation_id": "t1",
            "occurred_at": "2026-07-31T00:00:00+00:00",
        },
        {
            "record_type": "request_snapshot",
            "operation_id": "t1",
            "step_sequence": 1,
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


def test_span_metrics_expose_narrow_runtime_boundaries(tmp_path):
    trace_file = tmp_path / "s.jsonl"
    names = (
        "pickel.model_context.build",
        "pickel.model_request.semaphore_wait",
        "pickel.storage.request_content.write",
        "pickel.storage.response_content.write",
        "pickel.model_call.prepare_transaction",
        "pickel.model_call.complete_transaction",
        "pickel.tool.execute",
        "pickel.event.delivery",
    )
    trace_file.write_text(
        "\n".join(
            json.dumps(
                {
                    "record_type": "span",
                    "payload": {"name": name, "duration_ms": 1, "status": "ok"},
                }
            )
            for name in names
        )
        + "\n",
        encoding="utf-8",
    )

    enhancement = read_trace(trace_file)

    assert enhancement is not None
    assert all(
        enhancement.metrics[label]["count"] == 1
        for label in (
            "context",
            "model_semaphore",
            "request_content_write",
            "response_content_write",
            "model_prepare",
            "model_complete",
            "tool",
            "event_delivery",
        )
    )


def test_rotated_segments_are_read_before_active_file(tmp_path):
    active = tmp_path / "s.jsonl"
    rotated = tmp_path / "s.20260731T000000.0001.jsonl"
    rotated.write_text(
        json.dumps(
            {
                "event_type": "agent_run_started",
                "occurred_at": "2026-07-31T00:00:00+00:00",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    active.write_text(
        json.dumps(
            {
                "event_type": "agent_run_failed",
                "occurred_at": "2026-07-31T00:00:01+00:00",
                "error_type": "RuntimeError",
                "message": "boom",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    enhancement = read_trace(active)

    assert enhancement.agent_run_markers[0].failed["error_type"] == "RuntimeError"


def test_operation_trace_filter_excludes_global_and_other_operation_records(tmp_path):
    trace_file = tmp_path / "s.jsonl"
    records = [
        {
            "record_type": "span",
            "operation_id": "op-a",
            "payload": {"name": "pickel.tool.execute", "duration_ms": 5},
        },
        {
            "record_type": "span",
            "operation_id": "op-b",
            "payload": {"name": "pickel.tool.execute", "duration_ms": 7},
        },
        {
            "record_type": "runtime_event",
            "payload": {"name": "global"},
        },
    ]
    trace_file.write_text(
        "\n".join(json.dumps(record) for record in records) + "\n",
        encoding="utf-8",
    )

    data = read_operation_trace(trace_file, operation_id="op-a")

    assert len(data.spans) == 1
    assert data.spans[0]["duration_ms"] == 5
    assert data.runtime_events == []


def test_operation_trace_filter_applies_before_status_metrics(tmp_path):
    trace_file = tmp_path / "s.jsonl"
    records = [
        {
            "record_type": "runtime_event",
            "operation_id": "op-a",
            "mode": "standard",
            "trace_seq": 4,
            "event_type": "text_delta",
        },
        {
            "record_type": "diagnostic",
            "operation_id": "op-b",
            "mode": "full",
            "trace_seq": 99,
            "dropped_records": 8,
            "payload": {
                "name": "trace_delta_dropped",
                "attributes": {"records_dropped": 8},
            },
        },
    ]
    trace_file.write_text(
        "\n".join(json.dumps(record) for record in records) + "\n",
        encoding="utf-8",
    )

    data = read_operation_trace(trace_file, operation_id="op-a")

    assert data.trace_status["mode"] == "standard"
    assert data.trace_status["last_sequence"] == 4
    assert data.trace_status["dropped_records"] == 0
    assert data.trace_status["status_text"] == "Standard Trace 已读取 · 未报告丢弃"


def test_full_stream_summary_and_legacy_delta_counts_are_combined(tmp_path):
    trace_file = tmp_path / "s.jsonl"
    records = [
        {
            "record_type": "stream_delta_summary",
            "operation_id": "op-a",
            "mode": "full",
            "trace_seq": 4,
            "payload": {"delta_count": 120},
        },
        {
            "record_type": "runtime_event",
            "operation_id": "op-a",
            "mode": "full",
            "trace_seq": 5,
            "event_type": "text_delta",
        },
    ]
    trace_file.write_text(
        "\n".join(json.dumps(record) for record in records) + "\n",
        encoding="utf-8",
    )

    data = read_operation_trace(trace_file, operation_id="op-a")

    assert data.stream_deltas_count == 121
    assert data.trace_status["stream_deltas_captured"] is True
    assert (
        data.trace_status["status_text"]
        == "Full Trace 已读取 · 已汇总流式 delta · 未报告丢弃"
    )


def test_dropped_delta_count_uses_maximum_cumulative_value(tmp_path):
    trace_file = tmp_path / "s.jsonl"
    records = [
        {
            "record_type": "diagnostic",
            "operation_id": "op-a",
            "payload": {
                "name": "trace_delta_dropped",
                "attributes": {"delta_records_dropped": 2},
            },
        },
        {
            "record_type": "diagnostic",
            "operation_id": "op-a",
            "payload": {
                "name": "trace_delta_dropped",
                "attributes": {"delta_records_dropped": 9},
            },
        },
    ]
    trace_file.write_text(
        "\n".join(json.dumps(record) for record in records) + "\n",
        encoding="utf-8",
    )

    data = read_operation_trace(trace_file, operation_id="op-a")

    assert data.dropped_deltas_count == 9


def test_trace_status_记录模式并配对工具时序(tmp_path):
    trace_file = tmp_path / "s.jsonl"
    records = [
        {
            "record_type": "runtime_event",
            "mode": "standard",
            "trace_seq": 4,
            "operation_id": "op-a",
            "event_type": "tool_call_started",
            "tool_call_id": "call-1",
            "occurred_at": "2026-07-31T00:00:00+00:00",
        },
        {
            "record_type": "runtime_event",
            "mode": "standard",
            "trace_seq": 5,
            "operation_id": "op-a",
            "event_type": "tool_call_completed",
            "tool_call_id": "call-1",
            "occurred_at": "2026-07-31T00:00:01.250000+00:00",
        },
    ]
    trace_file.write_text(
        "\n".join(json.dumps(record) for record in records) + "\n",
        encoding="utf-8",
    )

    data = read_operation_trace(trace_file, operation_id="op-a")

    assert data.trace_status == {
        "mode": "standard",
        "available": True,
        "last_sequence": 5,
        "dropped_records": 0,
        "stream_deltas_captured": False,
        "status_text": "Standard Trace 已读取 · 未报告丢弃",
    }
    assert data.tool_timings["call-1"] == {
        "tool_call_id": "call-1",
        "source": "trace",
        "started_at": "2026-07-31T00:00:00+00:00",
        "finished_at": "2026-07-31T00:00:01.250000+00:00",
        "duration_ms": 1250.0,
        "missing": [],
        "partial": False,
    }


def test_tool_timing_缺少结束事件时明确_partial(tmp_path):
    trace_file = tmp_path / "s.jsonl"
    trace_file.write_text(
        json.dumps(
            {
                "record_type": "runtime_event",
                "mode": "standard",
                "operation_id": "op-a",
                "event_type": "tool_call_started",
                "tool_call_id": "call-1",
                "occurred_at": "2026-07-31T00:00:00+00:00",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    timing = read_operation_trace(trace_file, operation_id="op-a").tool_timings[
        "call-1"
    ]

    assert timing["partial"] is True
    assert timing["missing"] == ["finished_at"]
