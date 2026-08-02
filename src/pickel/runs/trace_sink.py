"""异步 JSONL trace：Runtime 的派生诊断轨迹，不参与对话恢复。"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pickel.config.paths import home_dir
from pickel.observe.records import (
    ObservationRecord,
    RequestSnapshotRecord,
    SpanRecord,
)
from pickel.runs.runtime_events import RuntimeEventBase

logger = logging.getLogger(__name__)

TRACE_ENV_VAR = "PICKEL_TRACE"
TRACE_MODE_ENV_VAR = "PICKEL_TRACE_MODE"
TraceMode = Literal["off", "standard", "full"]
_DELTA_EVENTS = {"thinking_delta", "text_delta", "tool_call_args_delta"}


def trace_mode(config_value: str | bool = "standard") -> TraceMode:
    """解析配置和环境覆盖；兼容旧布尔配置。"""
    override = os.environ.get(TRACE_MODE_ENV_VAR)
    if override is None:
        override = os.environ.get(TRACE_ENV_VAR)
    value: str | bool = override if override is not None else config_value
    if isinstance(value, bool):
        return "standard" if value else "off"
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return "standard"
    if normalized in {"0", "false", "no", "off"}:
        return "off"
    if normalized in {"standard", "full"}:
        return normalized  # type: ignore[return-value]
    return "standard"


def trace_enabled(config_value: str | bool = "standard") -> bool:
    return trace_mode(config_value) != "off"


def trace_path(session_id: str) -> Path:
    return home_dir() / "traces" / f"{session_id}.jsonl"


@dataclass(frozen=True)
class TraceOptions:
    mode: TraceMode = "standard"
    queue_capacity: int = 8192
    batch_size: int = 128
    flush_interval_ms: int = 250
    max_file_size_mb: int = 64
    max_age_days: int = 14
    max_total_size_mb: int = 1024


@dataclass
class _QueuedRecord:
    value: dict[str, Any]
    low_priority: bool = False
    flush_barrier: threading.Event | None = None


class _TraceBuffer:
    def __init__(self, capacity: int) -> None:
        self._capacity = max(1, capacity)
        self._items: deque[_QueuedRecord] = deque()
        self._condition = threading.Condition()
        self._closed = False

    def put(self, item: _QueuedRecord) -> tuple[bool, bool]:
        """返回 (已接收, 是否淘汰了一条低优先级记录)。"""
        with self._condition:
            if self._closed:
                return False, False
            evicted = False
            if len(self._items) >= self._capacity:
                if item.low_priority:
                    return False, False
                for index, queued in enumerate(self._items):
                    if queued.low_priority:
                        del self._items[index]
                        evicted = True
                        break
                else:
                    return False, False
            self._items.append(item)
            self._condition.notify()
            return True, evicted

    def take(self, limit: int, timeout_seconds: float) -> list[_QueuedRecord]:
        with self._condition:
            if not self._items and not self._closed:
                self._condition.wait(timeout_seconds)
            items: list[_QueuedRecord] = []
            while self._items and len(items) < limit:
                items.append(self._items.popleft())
            return items

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._condition.notify_all()

    def put_flush_barrier(self, barrier: threading.Event) -> bool:
        """控制记录不受容量限制，确保此前记录可被同步等待。"""
        with self._condition:
            if self._closed:
                return False
            self._items.append(_QueuedRecord(value={}, flush_barrier=barrier))
            self._condition.notify()
            return True

    @property
    def empty(self) -> bool:
        with self._condition:
            return not self._items

    @property
    def size(self) -> int:
        with self._condition:
            return len(self._items)


class JsonlTraceSink:
    """EventBus handler + Observer；调用方只入队，文件 I/O 在后台线程。"""

    SCHEMA_VERSION = 1

    def __init__(self, path: Path, options: TraceOptions | None = None) -> None:
        self._path = Path(path)
        self._options = options or TraceOptions()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # 提前验证路径可写，避免后台线程才发现初始化失败。
        self._handle = self._path.open("a", encoding="utf-8")
        self._buffer = _TraceBuffer(self._options.queue_capacity)
        self._sequence_lock = threading.Lock()
        self._next_trace_seq = 0
        self._closed = False
        self._write_errors = 0
        self._written = 0
        self._dropped = 0
        self._delta_dropped = 0
        self._queue_high_watermark = 0
        self._rotation_index = 0
        self._thread = threading.Thread(
            target=self._write_loop,
            name=f"pickel-trace-{self._path.stem}",
            daemon=True,
        )
        self._thread.start()

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def health(self) -> dict[str, int]:
        return {
            "records_written": self._written,
            "records_dropped": self._dropped,
            "delta_records_dropped": self._delta_dropped,
            "write_errors": self._write_errors,
            "queue_size": self._buffer.size,
            "queue_high_watermark": self._queue_high_watermark,
        }

    def __call__(self, event: RuntimeEventBase) -> None:
        if self._closed:
            return
        event_data = event.to_dict()
        event_type = str(event_data.get("event_type", ""))
        if self._options.mode == "standard" and event_type in _DELTA_EVENTS:
            return
        record = dict(event_data)
        record.update(
            {
                "schema_version": self.SCHEMA_VERSION,
                "record_type": "runtime_event",
                "recorded_at": _iso_now(),
            }
        )
        self._enqueue(record, low_priority=event_type in _DELTA_EVENTS)

    def record(self, observation: ObservationRecord) -> None:
        if self._closed:
            return
        if (
            isinstance(observation, RequestSnapshotRecord)
            and self._options.mode != "full"
        ):
            return
        identity = observation.identity
        if isinstance(observation, SpanRecord):
            record_type = "span"
        elif isinstance(observation, RequestSnapshotRecord):
            record_type = "request_snapshot"
        else:
            record_type = "diagnostic"
        record = {
            "schema_version": self.SCHEMA_VERSION,
            "record_type": record_type,
            "recorded_at": _iso_now(),
            "session_id": identity.session_id,
            "turn_id": identity.turn_id,
            "step_index": identity.step_index,
            "payload": observation.to_dict(),
        }
        self._enqueue(record, low_priority=False)

    def wants(self, capability: str) -> bool:
        return self._options.mode == "full" and capability == "request_snapshot"

    def flush(self, timeout_seconds: float = 5.0) -> bool:
        """等待当前已入队记录落盘，不关闭 sink。"""
        if self._closed:
            return False
        barrier = threading.Event()
        if not self._buffer.put_flush_barrier(barrier):
            return False
        return barrier.wait(timeout=max(0.0, timeout_seconds))

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._buffer.close()
        self._thread.join(timeout=5)
        if self._thread.is_alive():
            logger.warning("trace writer 未在 5 秒内退出: %s", self._path)
        if not self._handle.closed:
            self._handle.flush()
            self._handle.close()

    def _enqueue(self, record: dict[str, Any], *, low_priority: bool) -> None:
        with self._sequence_lock:
            record["trace_seq"] = self._next_trace_seq
            self._next_trace_seq += 1
        accepted, evicted = self._buffer.put(
            _QueuedRecord(value=record, low_priority=low_priority)
        )
        if evicted:
            self._dropped += 1
            self._delta_dropped += 1
        if not accepted:
            self._dropped += 1
            if low_priority:
                self._delta_dropped += 1
            return
        self._queue_high_watermark = max(self._queue_high_watermark, self._buffer.size)

    def _write_loop(self) -> None:
        timeout = max(0.01, self._options.flush_interval_ms / 1000)
        while not self._closed or not self._buffer.empty:
            batch = self._buffer.take(self._options.batch_size, timeout)
            if not batch:
                self._flush()
                continue
            barriers = [
                item.flush_barrier
                for item in batch
                if item.flush_barrier is not None
            ]
            try:
                lines = [
                    json.dumps(item.value, ensure_ascii=False, default=str) + "\n"
                    for item in batch
                    if item.flush_barrier is None
                ]
                if lines:
                    self._rotate_if_needed(
                        sum(len(line.encode("utf-8")) for line in lines)
                    )
                    self._handle.writelines(lines)
                    self._written += len(lines)
                self._flush()
            except Exception as exc:  # noqa: BLE001 — trace 永不阻断 Runtime
                self._write_errors += 1
                dropped = len(batch) - len(barriers)
                self._dropped += dropped
                logger.warning("trace 写入失败，已丢弃 %d 条记录: %s", dropped, exc)
            finally:
                for barrier in barriers:
                    barrier.set()
        self._flush()

    def _flush(self) -> None:
        try:
            if not self._handle.closed:
                self._handle.flush()
        except OSError as exc:
            self._write_errors += 1
            logger.warning("trace flush 失败: %s", exc)

    def _rotate_if_needed(self, incoming_bytes: int) -> None:
        max_bytes = max(1, self._options.max_file_size_mb) * 1024 * 1024
        try:
            current_size = self._path.stat().st_size
        except FileNotFoundError:
            current_size = 0
        if current_size == 0 or current_size + incoming_bytes <= max_bytes:
            return
        self._handle.flush()
        self._handle.close()
        self._rotation_index += 1
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        rotated = self._path.with_name(
            f"{self._path.stem}.{stamp}.{self._rotation_index:04d}{self._path.suffix}"
        )
        self._path.replace(rotated)
        self._handle = self._path.open("a", encoding="utf-8")
        self._apply_retention()

    def _apply_retention(self) -> None:
        segments = sorted(
            self._path.parent.glob(f"{self._path.stem}.*{self._path.suffix}"),
            key=lambda item: item.stat().st_mtime,
        )
        cutoff = time.time() - max(0, self._options.max_age_days) * 86400
        for segment in list(segments):
            if self._options.max_age_days > 0 and segment.stat().st_mtime < cutoff:
                segment.unlink(missing_ok=True)
                segments.remove(segment)
        max_total = max(1, self._options.max_total_size_mb) * 1024 * 1024
        total = (
            sum(item.stat().st_size for item in segments) + self._path.stat().st_size
        )
        for segment in segments:
            if total <= max_total:
                break
            size = segment.stat().st_size
            segment.unlink(missing_ok=True)
            total -= size


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()
