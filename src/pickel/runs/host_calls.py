"""Runtime 向宿主能力提供者发起的定向调用。"""

from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Generic, Protocol, TypeAlias, TypeVar
from uuid import uuid4

RequestT = TypeVar("RequestT")
ResponseT = TypeVar("ResponseT")


@dataclass(frozen=True)
class HostCallSpec(Generic[RequestT, ResponseT]):
    """稳定调用标识；name/version 同时也是远端 wire identity。"""

    name: str
    version: int
    request_type: type[RequestT]
    response_type: type[ResponseT]

    def __post_init__(self) -> None:
        if not self.name or not self.name.strip():
            raise ValueError("HostCallSpec.name 不能为空")
        if self.version < 1:
            raise ValueError("HostCallSpec.version 必须大于等于 1")

    @property
    def key(self) -> tuple[str, int]:
        return self.name, self.version


@dataclass(frozen=True)
class HostCallContext:
    call_id: str = field(default_factory=lambda: str(uuid4()))
    session_id: str = ""
    turn_id: str = ""
    step_index: int | None = None
    tool_call_id: str | None = None
    timeout_seconds: float | None = None


@dataclass(frozen=True)
class HostCallCompleted(Generic[ResponseT]):
    value: ResponseT


@dataclass(frozen=True)
class HostCallUnavailable:
    reason: str


@dataclass(frozen=True)
class HostCallCancelled:
    reason: str


@dataclass(frozen=True)
class HostCallDeadlineExceeded:
    reason: str = "deadline_exceeded"


@dataclass(frozen=True)
class HostCallFailed:
    error_type: str
    message: str


HostCallOutcome: TypeAlias = (
    HostCallCompleted[ResponseT]
    | HostCallUnavailable
    | HostCallCancelled
    | HostCallDeadlineExceeded
    | HostCallFailed
)


class HostCallClient(Protocol):
    def supports(self, spec: HostCallSpec[Any, Any]) -> bool: ...

    async def call(
        self,
        spec: HostCallSpec[RequestT, ResponseT],
        request: RequestT,
        context: HostCallContext,
    ) -> HostCallOutcome[ResponseT]: ...


class HostCallRecorder(Protocol):
    def record_started(
        self,
        spec: HostCallSpec[Any, Any],
        request: Any,
        context: HostCallContext,
    ) -> None | Awaitable[None]: ...

    def record_finished(
        self,
        spec: HostCallSpec[Any, Any],
        request: Any,
        context: HostCallContext,
        outcome: HostCallOutcome[Any],
    ) -> None | Awaitable[None]: ...


HostCallHandler: TypeAlias = Callable[
    [RequestT, HostCallContext], ResponseT | Awaitable[ResponseT]
]


class HostCallHandlerAlreadyRegisteredError(RuntimeError):
    pass


@dataclass
class _Registration:
    registration_id: int
    handler: HostCallHandler[Any, Any]


@dataclass
class _PendingCall:
    task: asyncio.Task[Any]
    registration_id: int
    cancel_reason: str | None = None


class HostCallHandlerLease:
    """一个 handler 注册租约；close 幂等。"""

    def __init__(self, close_callback: Callable[[], None]) -> None:
        self._close_callback = close_callback
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._close_callback()

    def __enter__(self) -> "HostCallHandlerLease":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


class _HostCallClientView:
    """只暴露发送权限，避免工具取得 handler 注册和 close 权限。"""

    def __init__(self, router: "HostCallRouter") -> None:
        self._router = router

    def supports(self, spec: HostCallSpec[Any, Any]) -> bool:
        return self._router.supports(spec)

    async def call(
        self,
        spec: HostCallSpec[RequestT, ResponseT],
        request: RequestT,
        context: HostCallContext,
    ) -> HostCallOutcome[ResponseT]:
        return await self._router.call(spec, request, context)


class HostCallRouter:
    """单进程定向调用路由；不认识 UI、MCP、Session 或重试策略。"""

    def __init__(self, recorder: HostCallRecorder | None = None) -> None:
        self._recorder = recorder
        self._registrations: dict[tuple[str, int], _Registration] = {}
        self._pending: dict[str, _PendingCall] = {}
        self._next_registration_id = 0
        self._closed = False
        self._client = _HostCallClientView(self)

    @property
    def client(self) -> HostCallClient:
        return self._client

    @property
    def closed(self) -> bool:
        return self._closed

    def supports(self, spec: HostCallSpec[Any, Any]) -> bool:
        return not self._closed and spec.key in self._registrations

    def register(
        self,
        spec: HostCallSpec[RequestT, ResponseT],
        handler: HostCallHandler[RequestT, ResponseT],
    ) -> HostCallHandlerLease:
        if self._closed:
            raise RuntimeError("HostCallRouter 已关闭")
        if spec.key in self._registrations:
            raise HostCallHandlerAlreadyRegisteredError(
                f"Host call handler 已注册: {spec.name}@{spec.version}"
            )
        registration = _Registration(self._next_registration_id, handler)
        self._next_registration_id += 1
        self._registrations[spec.key] = registration

        def unregister() -> None:
            current = self._registrations.get(spec.key)
            if (
                current is None
                or current.registration_id != registration.registration_id
            ):
                return
            self._registrations.pop(spec.key, None)
            self._cancel_registration_calls(
                registration.registration_id,
                reason="provider_detached",
            )

        return HostCallHandlerLease(unregister)

    async def call(
        self,
        spec: HostCallSpec[RequestT, ResponseT],
        request: RequestT,
        context: HostCallContext,
    ) -> HostCallOutcome[ResponseT]:
        if not isinstance(request, spec.request_type):
            return HostCallFailed(
                error_type="InvalidRequest",
                message=f"{spec.name}@{spec.version} request 类型不匹配",
            )
        if self._closed:
            return HostCallUnavailable("router_closed")
        if context.call_id in self._pending:
            return HostCallFailed(
                error_type="DuplicateCallId",
                message=f"重复的 Host call id: {context.call_id}",
            )

        await self._record_started(spec, request, context)
        registration = self._registrations.get(spec.key)
        if registration is None:
            outcome: HostCallOutcome[ResponseT] = HostCallUnavailable("no_handler")
            await self._record_finished(spec, request, context, outcome)
            return outcome

        # 外部 handler 在独立 task 中执行；Router 不持锁跨越 await。
        task = asyncio.create_task(
            self._invoke_handler(registration.handler, request, context),
            name=f"host-call-{spec.name}-{context.call_id}",
        )
        pending = _PendingCall(task=task, registration_id=registration.registration_id)
        self._pending[context.call_id] = pending
        try:
            try:
                if context.timeout_seconds is None:
                    response = await task
                else:
                    response = await asyncio.wait_for(
                        task,
                        timeout=context.timeout_seconds,
                    )
            except asyncio.TimeoutError:
                outcome = HostCallDeadlineExceeded()
            except asyncio.CancelledError:
                if pending.cancel_reason is None:
                    task.cancel()
                    outcome = HostCallCancelled("caller_cancelled")
                    await asyncio.shield(
                        self._record_finished(spec, request, context, outcome)
                    )
                    raise
                outcome = HostCallCancelled(pending.cancel_reason)
            except Exception as exc:  # noqa: BLE001 — handler 失败转成显式 outcome
                outcome = HostCallFailed(type(exc).__name__, str(exc))
            else:
                if not isinstance(response, spec.response_type):
                    outcome = HostCallFailed(
                        error_type="InvalidResponse",
                        message=f"{spec.name}@{spec.version} response 类型不匹配",
                    )
                else:
                    outcome = HostCallCompleted(response)

            await self._record_finished(spec, request, context, outcome)
            return outcome
        finally:
            current = self._pending.get(context.call_id)
            if current is pending:
                self._pending.pop(context.call_id, None)

    def close_now(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._registrations.clear()
        for pending in list(self._pending.values()):
            pending.cancel_reason = "router_closed"
            pending.task.cancel()

    async def close(self) -> None:
        self.close_now()
        tasks = [pending.task for pending in self._pending.values()]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _cancel_registration_calls(self, registration_id: int, *, reason: str) -> None:
        for pending in list(self._pending.values()):
            if pending.registration_id != registration_id:
                continue
            pending.cancel_reason = reason
            pending.task.cancel()

    @staticmethod
    async def _invoke_handler(
        handler: HostCallHandler[RequestT, ResponseT],
        request: RequestT,
        context: HostCallContext,
    ) -> ResponseT:
        response = handler(request, context)
        if inspect.isawaitable(response):
            return await response
        return response

    async def _record_started(
        self,
        spec: HostCallSpec[Any, Any],
        request: Any,
        context: HostCallContext,
    ) -> None:
        if self._recorder is None:
            return
        result = self._recorder.record_started(spec, request, context)
        if inspect.isawaitable(result):
            await result

    async def _record_finished(
        self,
        spec: HostCallSpec[Any, Any],
        request: Any,
        context: HostCallContext,
        outcome: HostCallOutcome[Any],
    ) -> None:
        if self._recorder is None:
            return
        result = self._recorder.record_finished(spec, request, context, outcome)
        if inspect.isawaitable(result):
            await result
