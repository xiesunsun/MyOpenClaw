"""异步 JSONL trace：Runtime 的派生诊断轨迹，不参与对话恢复。"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pickel.config.paths import home_dir
from pickel.shared.execution_identity import ExecutionIdentity
from pickel.telemetry.records import ObservationRecord, SpanRecord

if TYPE_CHECKING:
    from pickel.runtime.runtime_events import RuntimeEventBase


# 观测读取只需要这组标签；不要为路径/报告查询加载 Runtime 事件类型。
STREAM_DELTA_EVENT_TYPES = frozenset(
    {"thinking_delta", "text_delta", "tool_call_args_delta"}
)

logger = logging.getLogger(__name__)

TRACE_ENV_VAR = "PICKEL_TRACE"
TRACE_MODE_ENV_VAR = "PICKEL_TRACE_MODE"
TraceMode = Literal["off", "standard", "full"]
_TRACE_SEQUENCE_TAIL_BYTES = 64 * 1024


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
    flush_barrier: threading.Event | None = None


@dataclass
class _StreamDeltaSummary:
    """一个 ModelCall 的流式增量诊断摘要，不保存逐块正文。"""

    identity: ExecutionIdentity
    first_event_sequence: int | None = None
    last_event_sequence: int | None = None
    first_occurred_at: str | None = None
    last_occurred_at: str | None = None
    delta_count: int = 0
    text_count: int = 0
    text_chars: int = 0
    text_bytes: int = 0
    thinking_count: int = 0
    thinking_chars: int = 0
    thinking_bytes: int = 0
    tool_args_count: int = 0
    tool_args_chars: int = 0
    tool_args_bytes: int = 0

    def add(self, event: RuntimeEventBase) -> None:
        sequence = event.envelope.event_sequence
        occurred_at = event.envelope.occurred_at.isoformat()
        if self.delta_count == 0:
            self.first_event_sequence = sequence
            self.first_occurred_at = occurred_at
        self.last_event_sequence = sequence
        self.last_occurred_at = occurred_at
        self.delta_count += 1

        event_type = event.EVENT_TYPE
        if event_type == "text_delta":
            value = str(getattr(event, "text", ""))
            self.text_count += 1
            self.text_chars += len(value)
            self.text_bytes += len(value.encode("utf-8"))
        elif event_type == "thinking_delta":
            value = str(getattr(event, "text", ""))
            self.thinking_count += 1
            self.thinking_chars += len(value)
            self.thinking_bytes += len(value.encode("utf-8"))
        elif event_type == "tool_call_args_delta":
            value = str(getattr(event, "partial_json", ""))
            self.tool_args_count += 1
            self.tool_args_chars += len(value)
            self.tool_args_bytes += len(value.encode("utf-8"))

    def to_record(self, *, mode: TraceMode) -> dict[str, Any]:
        identity = self.identity
        record: dict[str, Any] = {
            "schema_version": JsonlTraceSink.SCHEMA_VERSION,
            "record_type": "stream_delta_summary",
            "mode": mode,
            "recorded_at": _iso_now(),
            "session_id": identity.session_id,
            "operation_id": identity.operation_id or "",
            "step_id": identity.step_id,
            "step_sequence": identity.step_sequence,
            "model_call_id": identity.model_call_id,
            "payload": {
                "first_event_sequence": self.first_event_sequence,
                "last_event_sequence": self.last_event_sequence,
                "first_occurred_at": self.first_occurred_at,
                "last_occurred_at": self.last_occurred_at,
                "delta_count": self.delta_count,
                "text": {
                    "count": self.text_count,
                    "chars": self.text_chars,
                    "utf8_bytes": self.text_bytes,
                },
                "thinking": {
                    "count": self.thinking_count,
                    "chars": self.thinking_chars,
                    "utf8_bytes": self.thinking_bytes,
                },
                "tool_call_args": {
                    "count": self.tool_args_count,
                    "chars": self.tool_args_chars,
                    "utf8_bytes": self.tool_args_bytes,
                },
            },
        }
        return record


class _TraceBuffer:
    def __init__(self, capacity: int) -> None:
        self._capacity = max(1, capacity)
        self._items: deque[_QueuedRecord] = deque()
        self._condition = threading.Condition()
        self._closed = False

    def put(self, item: _QueuedRecord) -> bool:
        with self._condition:
            if self._closed:
                return False
            if len(self._items) >= self._capacity:
                return False
            self._items.append(item)
            self._condition.notify()
            return True

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


def _next_trace_sequence(path: Path) -> int:
    """从所有 trace 段的尾部恢复文件级序号。

    只读取每个文件固定大小的尾部，避免激活时随历史正文线性增长；JSONL
    writer 总是在记录末尾写入 trace_seq，因此尾部足以恢复最新序号。崩溃
    留下的半行、旧 rotation 和无法读取的段都会被安全忽略。
    """
    maximum = -1
    candidates = {path}
    try:
        candidates.update(path.parent.glob(f"{path.stem}.*{path.suffix}"))
    except OSError:
        pass
    for candidate in sorted(candidates, key=lambda item: str(item)):
        try:
            with candidate.open("rb") as existing:
                existing.seek(0, os.SEEK_END)
                size = existing.tell()
                existing.seek(max(0, size - _TRACE_SEQUENCE_TAIL_BYTES))
                tail = existing.read(_TRACE_SEQUENCE_TAIL_BYTES)
        except OSError:
            continue
        # Reserve a sequence even when its record is only partially written:
        # reusing an already allocated sequence would make diagnostics
        # ambiguous after recovery.
        for match in re.finditer(rb'"trace_seq"\s*:\s*(\d+)', tail):
            maximum = max(maximum, int(match.group(1)))
        # Only complete JSONL records are otherwise candidates. A crash can
        # leave a partial final line; it is discarded before the next append.
        if not tail.endswith(b"\n"):
            tail = tail[: tail.rfind(b"\n") + 1] if b"\n" in tail else b""
    return maximum + 1


def _truncate_incomplete_tail(path: Path) -> None:
    """丢弃崩溃留下的未换行尾部，避免新记录拼接到半行。"""
    try:
        size = path.stat().st_size
        if size == 0:
            return
        with path.open("rb") as existing:
            existing.seek(size - 1)
            if existing.read(1) == b"\n":
                return
            offset = max(0, size - _TRACE_SEQUENCE_TAIL_BYTES)
            existing.seek(offset)
            tail = existing.read(size - offset)
        newline = tail.rfind(b"\n")
        with path.open("r+b") as writable:
            writable.truncate(offset + newline + 1 if newline >= 0 else 0)
    except FileNotFoundError:
        return
    except OSError as exc:
        logger.warning("trace 半行修复失败: %s", exc)


class JsonlTraceSink:
    """EventBus handler + Observer；调用方只入队，文件 I/O 在后台线程。"""

    SCHEMA_VERSION = 1
    _active_paths: set[Path] = set()
    _active_paths_lock = threading.Lock()

    def __init__(
        self,
        path: Path,
        options: TraceOptions | None = None,
    ) -> None:
        self._path = Path(path)
        self._options = options or TraceOptions()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        next_trace_seq = _next_trace_sequence(self._path)
        _truncate_incomplete_tail(self._path)
        # 提前验证路径可写，避免后台线程才发现初始化失败。
        self._handle = self._path.open("a", encoding="utf-8")
        self._active_path = self._path.resolve()
        with self._active_paths_lock:
            self._active_paths.add(self._active_path)
        self._apply_retention()
        self._buffer = _TraceBuffer(self._options.queue_capacity)
        self._sequence_lock = threading.Lock()
        self._next_trace_seq = next_trace_seq
        self._closed = False
        self._write_errors = 0
        self._written = 0
        self._dropped = 0
        self._queue_high_watermark = 0
        self._stream_lock = threading.Lock()
        self._stream_summaries: dict[tuple[object, ...], _StreamDeltaSummary] = {}
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
            "write_errors": self._write_errors,
            "queue_size": self._buffer.size,
            "queue_high_watermark": self._queue_high_watermark,
        }

    def __call__(self, event: RuntimeEventBase) -> None:
        if self._closed:
            return
        event_type = event.EVENT_TYPE
        if event_type in STREAM_DELTA_EVENT_TYPES:
            if self._options.mode == "full":
                self._record_stream_delta(event)
            return
        self._flush_stream_summaries()
        event_data = event.to_dict()
        record = dict(event_data)
        record.update(
            {
                "schema_version": self.SCHEMA_VERSION,
                "record_type": "runtime_event",
                "mode": self._options.mode,
                "recorded_at": _iso_now(),
            }
        )
        self._enqueue(record)

    def record(self, observation: ObservationRecord) -> None:
        if self._closed:
            return
        identity = observation.identity
        if isinstance(observation, SpanRecord):
            record_type = "span"
        else:
            record_type = "diagnostic"
        record = {
            "schema_version": self.SCHEMA_VERSION,
            "record_type": record_type,
            "mode": self._options.mode,
            "recorded_at": _iso_now(),
            "session_id": identity.session_id,
            "operation_id": identity.operation_id or "",
            "step_id": identity.step_id,
            "step_sequence": identity.step_sequence,
            "model_call_id": identity.model_call_id,
            "payload": observation.to_dict(),
        }
        # Span/Diagnostic 也必须保留完整执行身份；空的可选身份字段继续省略，
        # 兼容旧 Trace 的紧凑格式。
        if identity.tool_call_id is not None:
            record["tool_call_id"] = identity.tool_call_id
        if identity.message_id is not None:
            record["message_id"] = identity.message_id
        self._enqueue(record)

    def flush(self, timeout_seconds: float = 5.0) -> bool:
        """等待当前已入队记录落盘，不关闭 sink。"""
        if self._closed:
            return False
        self._flush_stream_summaries()
        barrier = threading.Event()
        if not self._buffer.put_flush_barrier(barrier):
            return False
        return barrier.wait(timeout=max(0.0, timeout_seconds))

    def close(self) -> None:
        if self._closed:
            return
        self._flush_stream_summaries()
        self._closed = True
        self._buffer.close()
        self._thread.join(timeout=5)
        if self._thread.is_alive():
            logger.warning("trace writer 未在 5 秒内退出: %s", self._path)
        if not self._handle.closed:
            self._handle.flush()
            self._handle.close()
        with self._active_paths_lock:
            self._active_paths.discard(self._active_path)

    def _record_stream_delta(
        self,
        event: RuntimeEventBase,
    ) -> None:
        identity = event.envelope.identity
        key = (
            identity.session_id,
            identity.operation_id,
            identity.step_id,
            identity.step_sequence,
            identity.model_call_id,
        )
        with self._stream_lock:
            summary = self._stream_summaries.get(key)
            if summary is None:
                summary = _StreamDeltaSummary(
                    identity=ExecutionIdentity(
                        session_id=identity.session_id,
                        operation_id=identity.operation_id,
                        step_id=identity.step_id,
                        step_sequence=identity.step_sequence,
                        model_call_id=identity.model_call_id,
                    )
                )
                self._stream_summaries[key] = summary
            summary.add(event)

    def _flush_stream_summaries(self) -> None:
        with self._stream_lock:
            summaries = tuple(self._stream_summaries.values())
            self._stream_summaries.clear()
        for summary in summaries:
            self._enqueue(summary.to_record(mode=self._options.mode))

    def _enqueue(self, record: dict[str, Any]) -> None:
        # 将累计丢弃数附在后续成功落盘的记录上，使 reader 不依赖进程内
        # health 状态也能呈现 Trace 的可丢失边界。
        record["dropped_records"] = self._dropped
        with self._sequence_lock:
            record["trace_seq"] = self._next_trace_seq
            self._next_trace_seq += 1
        accepted = self._buffer.put(_QueuedRecord(value=record))
        if not accepted:
            self._dropped += 1
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
                item.flush_barrier for item in batch if item.flush_barrier is not None
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
        while rotated.exists():
            self._rotation_index += 1
            rotated = self._path.with_name(
                f"{self._path.stem}.{stamp}.{self._rotation_index:04d}{self._path.suffix}"
            )
        self._path.replace(rotated)
        self._handle = self._path.open("a", encoding="utf-8")
        self._apply_retention()

    def _apply_retention(self) -> None:
        try:
            files = [
                item for item in self._path.parent.glob("*.jsonl") if item.is_file()
            ]
        except OSError as exc:
            logger.warning("trace retention 扫描失败: %s", exc)
            return
        with self._active_paths_lock:
            active_paths = set(self._active_paths)

        def is_active(segment: Path) -> bool:
            try:
                return segment.resolve() in active_paths
            except (OSError, RuntimeError):
                # retention 失败不能影响 trace 写入；未知路径按 active
                # 处理，宁可暂时保留也不误删正在写入的文件。
                return True

        def sort_key(item: Path) -> tuple[int, str]:
            try:
                modified = item.stat().st_mtime_ns
            except OSError:
                modified = 0
            return modified, str(item)

        files.sort(key=sort_key)
        cutoff = time.time() - max(0, self._options.max_age_days) * 86400
        retained: list[tuple[Path, int]] = []
        for segment in files:
            try:
                size = segment.stat().st_size
                modified = segment.stat().st_mtime
            except OSError:
                continue
            if (
                not is_active(segment)
                and self._options.max_age_days > 0
                and modified < cutoff
            ):
                try:
                    segment.unlink(missing_ok=True)
                except OSError as exc:
                    logger.warning("trace 过期文件删除失败: %s", exc)
                    retained.append((segment, size))
                continue
            retained.append((segment, size))

        max_total = max(1, self._options.max_total_size_mb) * 1024 * 1024
        total = sum(size for _, size in retained)
        for segment, size in retained:
            if total <= max_total:
                break
            if is_active(segment):
                continue
            try:
                segment.unlink(missing_ok=True)
            except OSError as exc:
                logger.warning("trace retention 文件删除失败: %s", exc)
                continue
            total -= size


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()
