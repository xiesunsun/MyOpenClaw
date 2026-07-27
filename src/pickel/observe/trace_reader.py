"""trace JSONL → 时序增强(非真源)。

红线 5:禁止从 trace 重建对话或用量。本模块逐字段白名单读取,
只取 Session 里没有的时间戳与终态事件:
    event_type / seq / occurred_at / tool_call.id / error_type /
    message(仅 turn_failed) / at_step
绝不读取 user_text / text / usage / arguments / content / partial_json。
"""

from __future__ import annotations

import json
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


@dataclass(frozen=True)
class TraceEnhancement:
    tool_timings: dict[str, ToolTiming] = field(default_factory=dict)
    turn_markers: list[TurnMarker] = field(default_factory=list)
    # 按 turn_started 分组的 request_digest 序列;digest 本身即摘要
    # (长度/名称/条数),发射端(RequestDigestEvent)保证无正文。
    request_digests: list[list[dict]] = field(default_factory=list)


def read_trace(path: Path) -> TraceEnhancement | None:
    if not path.exists():
        return None

    started: dict[str, str] = {}
    timings: dict[str, ToolTiming] = {}
    markers: list[_MarkerBuilder] = []

    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue

        event_type = event.get("event_type")
        occurred_at = event.get("occurred_at")

        if event_type == "turn_started":
            markers.append(_MarkerBuilder(started_at=occurred_at))
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

    return TraceEnhancement(
        tool_timings=timings,
        turn_markers=[builder.freeze() for builder in markers],
        request_digests=[builder.digests for builder in markers],
    )


class _MarkerBuilder:
    def __init__(self, *, started_at: str | None) -> None:
        self.started_at = started_at
        self.failed: dict[str, str] | None = None
        self.interrupted = False
        self.digests: list[dict] = []

    def freeze(self) -> TurnMarker:
        return TurnMarker(
            started_at=self.started_at,
            failed=self.failed,
            interrupted=self.interrupted,
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
        "tool_names": [
            str(name) for name in (event.get("tool_names") or [])
        ],
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
