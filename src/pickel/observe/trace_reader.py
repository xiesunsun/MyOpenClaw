"""trace JSONL → 时序与运行指标增强（非对话真源）。

对 RuntimeEvent 仍按白名单读取，禁止从 trace 重建对话；新增 span 只用于
耗时、成功率、TTFT 与 token/cache 聚合。
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


@dataclass(frozen=True)
class ToolTiming:
    started_at: str | None
    completed_at: str | None
    duration_ms: int | None


@dataclass(frozen=True)
class TurnMarker:
    started_at: str | None
    failed: dict[str, str] | None
    interrupted: bool
    duration_ms: int | None = None
    outcome: str | None = None


@dataclass(frozen=True)
class TraceEnhancement:
    tool_timings: dict[str, ToolTiming] = field(default_factory=dict)
    turn_markers: list[TurnMarker] = field(default_factory=list)
    # 按 turn_started 分组的 request_digest 序列;digest 本身即摘要
    # (长度/名称/条数),发射端(RequestDigestEvent)保证无正文。
    request_digests: list[list[dict]] = field(default_factory=list)
    request_snapshots: list[list[dict]] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)


def read_trace(path: Path) -> TraceEnhancement | None:
    files = _trace_files(path)
    if not files:
        return None

    started: dict[str, str] = {}
    timings: dict[str, ToolTiming] = {}
    markers: list[_MarkerBuilder] = []
    spans: list[dict] = []

    for line in _trace_lines(files):
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue

        event_type = event.get("event_type")
        occurred_at = event.get("occurred_at")

        if event.get("record_type") == "span" and isinstance(
            event.get("payload"), dict
        ):
            span = dict(event["payload"])
            span["_turn_id"] = event.get("turn_id")
            spans.append(span)
            continue

        if event.get("record_type") == "request_snapshot" and isinstance(
            event.get("payload"), dict
        ):
            turn_id = str(event.get("turn_id") or "")
            marker = next(
                (item for item in reversed(markers) if item.turn_id == turn_id),
                None,
            )
            if marker is not None:
                marker.snapshots.append(dict(event["payload"]))
            continue

        if event_type == "turn_started":
            markers.append(
                _MarkerBuilder(
                    started_at=occurred_at,
                    turn_id=str(event.get("turn_id") or ""),
                )
            )
        elif event_type == "tool_call_started":
            call_id = _call_id(event)
            if call_id and occurred_at:
                started[call_id] = occurred_at
        elif event_type == "tool_call_completed":
            call_id = _call_id(event)
            if call_id:
                begin = started.get(call_id)
                timings[call_id] = ToolTiming(
                    started_at=begin,
                    completed_at=occurred_at,
                    duration_ms=_duration_ms(begin, occurred_at),
                )
        elif event_type == "turn_failed" and markers:
            markers[-1].failed = {
                "error_type": str(event.get("error_type") or ""),
                "message": str(event.get("message") or ""),
            }
        elif event_type == "turn_interrupted" and markers:
            markers[-1].interrupted = True
        elif event_type == "request_digest" and markers:
            markers[-1].digests.append(_digest_fields(event))

    turn_spans = {
        str(span.get("_turn_id") or ""): span
        for span in spans
        if span.get("name") == "pickel.turn"
    }
    for marker in markers:
        span = turn_spans.get(marker.turn_id)
        if span is not None:
            duration = span.get("duration_ms")
            marker.duration_ms = (
                round(float(duration)) if isinstance(duration, (int, float)) else None
            )
            attributes = span.get("attributes")
            if isinstance(attributes, dict):
                marker.outcome = str(attributes.get("outcome") or "") or None

    for span in spans:
        if span.get("name") != "pickel.tool.execute":
            continue
        attributes = span.get("attributes")
        duration = span.get("duration_ms")
        if not isinstance(attributes, dict) or not isinstance(duration, (int, float)):
            continue
        call_id = attributes.get("tool_call_id")
        if isinstance(call_id, str) and call_id in timings:
            timing = timings[call_id]
            timings[call_id] = ToolTiming(
                started_at=timing.started_at,
                completed_at=timing.completed_at,
                duration_ms=round(float(duration)),
            )

    return TraceEnhancement(
        tool_timings=timings,
        turn_markers=[builder.freeze() for builder in markers],
        request_digests=[builder.digests for builder in markers],
        request_snapshots=[builder.snapshots for builder in markers],
        metrics=_build_metrics(spans),
    )


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
        "turn": "pickel.turn",
        "provider": "pickel.provider.request",
        "tool": "pickel.tool.execute",
        "hook": "pickel.hook.",
        "context": "pickel.model_context.build",
        "storage": "pickel.session.append",
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
    def __init__(self, *, started_at: str | None, turn_id: str = "") -> None:
        self.started_at = started_at
        self.turn_id = turn_id
        self.failed: dict[str, str] | None = None
        self.interrupted = False
        self.digests: list[dict] = []
        self.snapshots: list[dict] = []
        self.duration_ms: int | None = None
        self.outcome: str | None = None

    def freeze(self) -> TurnMarker:
        return TurnMarker(
            started_at=self.started_at,
            failed=self.failed,
            interrupted=self.interrupted,
            duration_ms=self.duration_ms,
            outcome=self.outcome,
        )


def _digest_fields(event: dict) -> dict:
    """白名单提取 digest:只收数值与名称字段,逐项重建不透传原 dict。"""
    sections = []
    for section in event.get("system_sections") or []:
        if isinstance(section, dict):
            sections.append(
                {
                    "name": str(section.get("name") or ""),
                    "chars": int(section.get("chars") or 0),
                }
            )
    return {
        "system_sections": sections,
        "tool_names": [str(name) for name in (event.get("tool_names") or [])],
        "message_count": int(event.get("message_count") or 0),
        "request_chars": int(event.get("request_chars") or 0),
        "hook_injected_chars": int(event.get("hook_injected_chars") or 0),
    }


def _call_id(event: dict) -> str | None:
    tool_call = event.get("tool_call")
    if isinstance(tool_call, dict):
        call_id = tool_call.get("id")
        if isinstance(call_id, str):
            return call_id
    return None


def _duration_ms(begin: str | None, end: str | None) -> int | None:
    if not begin or not end:
        return None
    try:
        delta = datetime.fromisoformat(end) - datetime.fromisoformat(begin)
    except ValueError:
        return None
    return int(delta.total_seconds() * 1000)
