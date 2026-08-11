"""Runtime 诊断记录与被动 Observer 端口。"""

from __future__ import annotations

import time
import traceback
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterator, Literal, Protocol
from uuid import uuid4


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class ErrorInfo:
    """跨 Hook、provider 与 tool 共用的结构化异常。"""

    kind: str
    type: str
    message: str
    traceback: str = ""
    retryable: bool | None = None

    @classmethod
    def from_exception(
        cls,
        exc: BaseException,
        *,
        kind: str = "exception",
        retryable: bool | None = None,
    ) -> ErrorInfo:
        return cls(
            kind=kind,
            type=type(exc).__name__,
            message=str(exc),
            traceback="".join(
                traceback.format_exception(type(exc), exc, exc.__traceback__)
            ),
            retryable=retryable,
        )


@dataclass(frozen=True)
class ObservationIdentity:
    session_id: str = ""
    operation_id: str = ""
    step_id: str | None = None
    step_sequence: int | None = None


@dataclass(frozen=True)
class SpanRecord:
    """一次已完成操作；单记录避免 start/end 配对复杂度。"""

    name: str
    identity: ObservationIdentity = field(default_factory=ObservationIdentity)
    span_id: str = field(default_factory=lambda: str(uuid4()))
    parent_span_id: str | None = None
    started_at: datetime = field(default_factory=_now)
    finished_at: datetime = field(default_factory=_now)
    duration_ms: float = 0
    status: Literal["ok", "error", "cancelled", "denied"] = "ok"
    attributes: dict[str, Any] = field(default_factory=dict)
    error: ErrorInfo | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "span_id": self.span_id,
            "parent_span_id": self.parent_span_id,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat(),
            "duration_ms": self.duration_ms,
            "status": self.status,
            "attributes": dict(self.attributes),
            "error": asdict(self.error) if self.error is not None else None,
        }


@dataclass(frozen=True)
class DiagnosticRecord:
    name: str
    identity: ObservationIdentity = field(default_factory=ObservationIdentity)
    occurred_at: datetime = field(default_factory=_now)
    level: Literal["debug", "info", "warning", "error"] = "error"
    attributes: dict[str, Any] = field(default_factory=dict)
    error: ErrorInfo | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "occurred_at": self.occurred_at.isoformat(),
            "level": self.level,
            "attributes": dict(self.attributes),
            "error": asdict(self.error) if self.error is not None else None,
        }


@dataclass(frozen=True)
class RequestSnapshotRecord:
    """完整 Provider 请求快照；仅由 full trace 接收。"""

    provider: str
    model: str
    request: dict[str, Any]
    identity: ObservationIdentity = field(default_factory=ObservationIdentity)
    cache_order: tuple[str, ...] = ()
    occurred_at: datetime = field(default_factory=_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "occurred_at": self.occurred_at.isoformat(),
            "cache_order": list(self.cache_order),
            "request": self.request,
        }


ObservationRecord = SpanRecord | DiagnosticRecord | RequestSnapshotRecord


class Observer(Protocol):
    """只接收事实；实现必须快速返回且不得影响 Runtime。"""

    def record(self, record: ObservationRecord) -> None: ...


class NoopObserver:
    def record(self, record: ObservationRecord) -> None:
        return None

    def wants(self, capability: str) -> bool:
        return False


_NOOP_OBSERVER = NoopObserver()
_CURRENT_OBSERVER: ContextVar[Observer] = ContextVar(
    "pickel_observer", default=_NOOP_OBSERVER
)
_CURRENT_SPAN_ID: ContextVar[str | None] = ContextVar(
    "pickel_parent_span_id", default=None
)


def current_observer() -> Observer:
    return _CURRENT_OBSERVER.get()


def observation_requested(capability: str) -> bool:
    """询问 Observer 是否需要昂贵的可选数据；旧 Observer 默认不需要。"""
    wants = getattr(current_observer(), "wants", None)
    if not callable(wants):
        return False
    try:
        return bool(wants(capability))
    except Exception:
        return False


@contextmanager
def observation_scope(observer: Observer | None) -> Iterator[None]:
    token = _CURRENT_OBSERVER.set(observer or _NOOP_OBSERVER)
    try:
        yield
    finally:
        _CURRENT_OBSERVER.reset(token)


@contextmanager
def span_scope(span_id: str) -> Iterator[None]:
    token = _CURRENT_SPAN_ID.set(span_id)
    try:
        yield
    finally:
        _CURRENT_SPAN_ID.reset(token)


class SpanTimer:
    """低开销计时器；finish 最多记录一次。"""

    def __init__(
        self,
        name: str,
        identity: ObservationIdentity,
        *,
        attributes: dict[str, Any] | None = None,
        parent_span_id: str | None = None,
    ) -> None:
        self.name = name
        self.identity = identity
        self.span_id = str(uuid4())
        self.parent_span_id = parent_span_id or _CURRENT_SPAN_ID.get()
        self.started_at = _now()
        self._started = time.perf_counter()
        self._attributes = dict(attributes or {})
        self._finished = False

    def finish(
        self,
        *,
        status: Literal["ok", "error", "cancelled", "denied"] = "ok",
        attributes: dict[str, Any] | None = None,
        error: ErrorInfo | None = None,
    ) -> None:
        if self._finished:
            return
        self._finished = True
        merged = dict(self._attributes)
        merged.update(attributes or {})
        finished_at = _now()
        record = SpanRecord(
            name=self.name,
            identity=self.identity,
            span_id=self.span_id,
            parent_span_id=self.parent_span_id,
            started_at=self.started_at,
            finished_at=finished_at,
            duration_ms=round((time.perf_counter() - self._started) * 1000, 3),
            status=status,
            attributes=merged,
            error=error,
        )
        try:
            current_observer().record(record)
        except Exception:
            # Observer 是被动诊断端口，任何实现错误都不能进入执行路径。
            return


def record_diagnostic(record: DiagnosticRecord) -> None:
    try:
        current_observer().record(record)
    except Exception:
        return


def record_request_snapshot(record: RequestSnapshotRecord) -> None:
    try:
        current_observer().record(record)
    except Exception:
        return
