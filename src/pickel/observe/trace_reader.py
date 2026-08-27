"""trace JSONL → 时序与运行指标增强（非对话真源）。

对 RuntimeEvent 仍按白名单读取，禁止从 trace 重建对话；
span、stream delta 和 diagnostic 只用于耗时、成功率、TTFT 与时序诊断。
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


@dataclass(frozen=True)
class AgentRunMarker:
    started_at: str | None
    failed: dict[str, str] | None
    interrupted: bool
    duration_ms: int | None = None
    outcome: str | None = None


@dataclass(frozen=True)
class TraceEnhancement:
    agent_run_markers: list[AgentRunMarker] = field(default_factory=list)
    request_snapshots: list[list[dict]] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)
    trace_status: dict = field(default_factory=dict)


@dataclass(frozen=True)
class OperationTraceData:
    """单个 Operation / Session 读取到的可丢失诊断轨迹。"""

    trace_available: bool
    spans: list[dict] = field(default_factory=list)
    runtime_events: list[dict] = field(default_factory=list)
    diagnostics: list[dict] = field(default_factory=list)
    stream_deltas_count: int = 0
    dropped_deltas_count: int = 0
    metrics: dict = field(default_factory=dict)
    tool_timings: dict[str, dict] = field(default_factory=dict)
    trace_status: dict = field(default_factory=dict)

    @property
    def integrity_summary(self) -> str:
        return str(self.trace_status.get("status_text") or "Trace 状态未知")


def read_trace(path: Path) -> TraceEnhancement | None:
    files = _trace_files(path)
    if not files:
        return None

    markers: list[_MarkerBuilder] = []
    spans: list[dict] = []
    mode: str | None = None
    last_sequence = -1
    dropped_records = 0
    stream_deltas = 0

    for line in _trace_lines(files):
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        mode = _read_trace_mode(event, mode)
        last_sequence = max(last_sequence, _read_trace_sequence(event))
        dropped_records = max(
            dropped_records,
            _read_number(event.get("dropped_records")),
        )

        event_type = event.get("event_type")
        occurred_at = event.get("occurred_at")

        if event.get("record_type") == "span" and isinstance(
            event.get("payload"), dict
        ):
            span = dict(event["payload"])
            span["_operation_id"] = event.get("operation_id")
            spans.append(span)
            continue

        if event.get("event_type") in {
            "thinking_delta",
            "text_delta",
            "tool_call_args_delta",
        }:
            stream_deltas += 1
        if event.get("record_type") == "diagnostic" and isinstance(
            event.get("payload"), dict
        ):
            attributes = event["payload"].get("attributes")
            if isinstance(attributes, dict):
                dropped_records = max(
                    dropped_records,
                    _read_number(attributes.get("records_dropped")),
                )

        if event.get("record_type") == "request_snapshot" and isinstance(
            event.get("payload"), dict
        ):
            operation_id = str(event.get("operation_id") or "")
            marker = next(
                (
                    item
                    for item in reversed(markers)
                    if item.operation_id == operation_id
                ),
                None,
            )
            if marker is not None:
                marker.snapshots.append(dict(event["payload"]))
            continue

        if event_type == "agent_run_started":
            markers.append(
                _MarkerBuilder(
                    started_at=occurred_at,
                    operation_id=str(event.get("operation_id") or ""),
                )
            )
        elif event_type == "agent_run_failed" and markers:
            markers[-1].failed = {
                "error_type": str(event.get("error_type") or ""),
                "message": str(event.get("message") or ""),
            }
        elif event_type == "agent_run_interrupted" and markers:
            markers[-1].interrupted = True

    agent_run_spans = {
        str(span.get("_operation_id") or ""): span
        for span in spans
        if span.get("name") == "pickel.agent_run"
    }
    for marker in markers:
        span = agent_run_spans.get(marker.operation_id)
        if span is not None:
            duration = span.get("duration_ms")
            marker.duration_ms = (
                round(float(duration)) if isinstance(duration, (int, float)) else None
            )
            attributes = span.get("attributes")
            if isinstance(attributes, dict):
                marker.outcome = str(attributes.get("outcome") or "") or None

    return TraceEnhancement(
        agent_run_markers=[builder.freeze() for builder in markers],
        request_snapshots=[builder.snapshots for builder in markers],
        metrics=_build_metrics(spans),
        trace_status=_build_trace_status(
            mode=mode,
            available=True,
            last_sequence=last_sequence,
            dropped_records=dropped_records,
            stream_deltas_count=stream_deltas,
        ),
    )


def read_operation_trace(
    path: Path, *, operation_id: str | None = None
) -> OperationTraceData:
    files = _trace_files(path)
    if not files:
        return OperationTraceData(
            trace_available=False,
            trace_status=_build_trace_status(
                mode=None,
                available=False,
                last_sequence=-1,
                dropped_records=0,
                stream_deltas_count=0,
            ),
        )

    spans: list[dict] = []
    events: list[dict] = []
    diagnostics: list[dict] = []
    stream_deltas = 0
    dropped_deltas = 0
    dropped_records = 0
    mode: str | None = None
    last_sequence = -1

    for line in _trace_lines(files):
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue

        mode = _read_trace_mode(record, mode)
        last_sequence = max(last_sequence, _read_trace_sequence(record))
        dropped_records = max(
            dropped_records,
            _read_number(record.get("dropped_records")),
        )

        rec_op_id = record.get("operation_id")
        # Operation 过滤必须是精确匹配。没有 operation_id 的全局记录不能
        # 混入某一个 Operation，否则观测页会把别的运行的 span/delta 算进来。
        if operation_id is not None and rec_op_id != operation_id:
            continue

        record_type = record.get("record_type", "runtime_event")
        if record_type == "span" and isinstance(record.get("payload"), dict):
            span = dict(record["payload"])
            span["_operation_id"] = record.get("operation_id")
            span["_step_id"] = record.get("step_id")
            spans.append(span)
        elif record_type == "diagnostic" and isinstance(record.get("payload"), dict):
            diag = dict(record["payload"])
            diagnostics.append(diag)
            diagnostic_name = str(diag.get("name", ""))
            if "dropped_delta" in diagnostic_name or "delta_dropped" in diagnostic_name:
                dropped_deltas = max(
                    dropped_deltas,
                    _read_dropped_delta_count(diag),
                )
            attributes = diag.get("attributes")
            if isinstance(attributes, dict):
                dropped_records = max(
                    dropped_records,
                    _read_number(attributes.get("records_dropped")),
                )
        else:
            event_type = str(record.get("event_type") or "")
            if event_type in {"thinking_delta", "text_delta", "tool_call_args_delta"}:
                stream_deltas += 1
            else:
                events.append(record)

    tool_timings = _pair_tool_timings(events)
    return OperationTraceData(
        trace_available=True,
        spans=spans,
        runtime_events=events,
        diagnostics=diagnostics,
        stream_deltas_count=stream_deltas,
        dropped_deltas_count=dropped_deltas,
        metrics=_build_metrics(spans),
        tool_timings=tool_timings,
        trace_status=_build_trace_status(
            mode=mode,
            available=True,
            last_sequence=last_sequence,
            dropped_records=dropped_records,
            stream_deltas_count=stream_deltas,
        ),
    )


def _read_trace_mode(record: dict, current: str | None) -> str | None:
    value = record.get("mode") or record.get("trace_mode")
    if value in {"standard", "full"}:
        return str(value)
    return current


def _read_trace_sequence(record: dict) -> int:
    value = record.get("trace_seq")
    return int(value) if isinstance(value, (int, float)) else -1


def _read_number(value: object) -> int:
    return max(0, int(value)) if isinstance(value, (int, float)) else 0


def _build_trace_status(
    *,
    mode: str | None,
    available: bool,
    last_sequence: int,
    dropped_records: int,
    stream_deltas_count: int,
) -> dict:
    resolved_mode = mode if mode in {"standard", "full"} else "standard"
    stream_captured = resolved_mode == "full" and stream_deltas_count > 0
    if not available:
        status_text = "Trace 数据未采集或已清理"
    elif dropped_records > 0:
        status_text = (
            f"{resolved_mode.title()} Trace 已读取 · 丢弃 {dropped_records} 条记录"
        )
    elif resolved_mode == "standard":
        status_text = "Standard Trace 已读取 · 未报告丢弃"
    elif stream_captured:
        status_text = "Full Trace 已读取 · 已捕获流式 delta · 未报告丢弃"
    else:
        status_text = "Full Trace 已读取 · 未发现流式 delta · 未报告丢弃"
    return {
        "mode": resolved_mode,
        "available": available,
        "last_sequence": last_sequence if last_sequence >= 0 else None,
        "dropped_records": dropped_records,
        "stream_deltas_captured": stream_captured,
        "status_text": status_text,
    }


def _pair_tool_timings(events: list[dict]) -> dict[str, dict]:
    """把 RuntimeEvent 的工具起止通知配成按 tool_call_id 的诊断时序。"""

    timings: dict[str, dict] = {}
    for event in events:
        event_type = event.get("event_type")
        if event_type not in {"tool_call_started", "tool_call_completed"}:
            continue
        tool_call_id = event.get("tool_call_id")
        if not tool_call_id:
            continue
        item = timings.setdefault(
            str(tool_call_id),
            {
                "tool_call_id": str(tool_call_id),
                "source": "trace",
                "started_at": None,
                "finished_at": None,
                "duration_ms": None,
            },
        )
        occurred_at = event.get("occurred_at")
        if event_type == "tool_call_started" and item["started_at"] is None:
            item["started_at"] = occurred_at
        elif event_type == "tool_call_completed" and item["finished_at"] is None:
            item["finished_at"] = occurred_at

    for item in timings.values():
        started = _parse_datetime(item["started_at"])
        finished = _parse_datetime(item["finished_at"])
        if started is not None and finished is not None:
            item["duration_ms"] = round(
                max(0.0, (finished - started).total_seconds() * 1000.0), 3
            )
        missing = [
            field for field in ("started_at", "finished_at") if item[field] is None
        ]
        item["missing"] = missing
        item["partial"] = bool(missing)
    return timings


def _parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _read_dropped_delta_count(diagnostic: dict) -> int:
    """读取 TraceSink 写入的累计丢弃数，兼容早期单次 count。"""

    attributes = diagnostic.get("attributes")
    if not isinstance(attributes, dict):
        return 1
    cumulative = attributes.get("delta_records_dropped")
    if isinstance(cumulative, (int, float)):
        return max(0, int(cumulative))
    legacy_count = attributes.get("count")
    if isinstance(legacy_count, (int, float)):
        return max(0, int(legacy_count))
    return 1


def _trace_files(path: Path) -> list[Path]:
    rotated = sorted(
        path.parent.glob(f"{path.stem}.*{path.suffix}"),
        key=lambda item: item.stat().st_mtime,
    )
    if path.exists():
        rotated.append(path)
    return rotated


def _trace_lines(files: list[Path]):
    for path in files:
        try:
            yield from path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue


def _build_metrics(spans: list[dict]) -> dict:
    groups = {
        "agent_run": "pickel.agent_run",
        "provider": "pickel.provider.request",
        "tool": "pickel.tool.execute",
        "hook": "pickel.hook.",
        "context": "pickel.model_context.build",
        "storage": "pickel.storage.commit",
    }
    result: dict[str, dict] = {}
    for label, name in groups.items():
        matched = [
            span
            for span in spans
            if (
                str(span.get("name", "")).startswith(name)
                if name.endswith(".")
                else span.get("name") == name
            )
        ]
        durations = [
            float(span["duration_ms"])
            for span in matched
            if isinstance(span.get("duration_ms"), (int, float))
        ]
        successes = sum(span.get("status") == "ok" for span in matched)
        summary = {
            "count": len(matched),
            "success_count": successes,
            "failure_count": len(matched) - successes,
            "success_rate": round(successes / len(matched), 4) if matched else None,
            "duration_ms": _percentiles(durations),
        }
        if label == "provider":
            ttft = [
                float(attributes["ttft_ms"])
                for span in matched
                if isinstance((attributes := span.get("attributes")), dict)
                and isinstance(attributes.get("ttft_ms"), (int, float))
            ]
            summary["ttft_ms"] = _percentiles(ttft)
            summary["tokens"] = {
                key: sum(
                    int(attributes.get(key) or 0)
                    for span in matched
                    if isinstance((attributes := span.get("attributes")), dict)
                )
                for key in (
                    "input_tokens",
                    "output_tokens",
                    "cache_read_tokens",
                    "cache_write_tokens",
                )
            }
        result[label] = summary
    return result


def _percentiles(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {"p50": None, "p95": None, "p99": None}
    ordered = sorted(values)

    def nearest(percentile: float) -> float:
        index = max(0, math.ceil(percentile * len(ordered)) - 1)
        return round(ordered[index], 3)

    return {"p50": nearest(0.5), "p95": nearest(0.95), "p99": nearest(0.99)}


class _MarkerBuilder:
    def __init__(self, *, started_at: str | None, operation_id: str = "") -> None:
        self.started_at = started_at
        self.operation_id = operation_id
        self.failed: dict[str, str] | None = None
        self.interrupted = False
        self.snapshots: list[dict] = []
        self.duration_ms: int | None = None
        self.outcome: str | None = None

    def freeze(self) -> AgentRunMarker:
        return AgentRunMarker(
            started_at=self.started_at,
            failed=self.failed,
            interrupted=self.interrupted,
            duration_ms=self.duration_ms,
            outcome=self.outcome,
        )
