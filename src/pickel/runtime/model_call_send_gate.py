"""ModelCall prepared→in_flight CAS 之后的唯一 Provider 发送闸门。"""

from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Awaitable, Callable

from pickel.model_calls.model_call import ModelCall
from pickel.model_calls.prepared import PreparedModelCall
from pickel.model_calls.service import ModelCallResponse
from pickel.model_calls.store import ModelCallStore
from pickel.providers.stream import StreamCompleted, StreamDelta
from pickel.runtime.runtime_effects import RuntimeEffects
from pickel.shared.execution_identity import ExecutionIdentity

StreamDeltaConsumer = Callable[[StreamDelta, ExecutionIdentity], None | Awaitable[None]]


class ModelCallSendConflict(RuntimeError):
    """发送闸门 CAS 失败，因此没有调用 Provider。"""


@dataclass(frozen=True)
class ModelCallSendFailure(Exception):
    call: ModelCall
    cause: Exception
    first_chunk_at: datetime | None

    def __str__(self) -> str:
        return str(self.cause)


class ModelCallSendGate:
    """先 CAS in_flight，再消费同一个 PreparedModelCall。"""

    def __init__(
        self,
        store: ModelCallStore,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._now = now or (lambda: datetime.now(timezone.utc))

    async def send(
        self,
        *,
        call: ModelCall,
        prepared: PreparedModelCall,
        effects: RuntimeEffects,
        consume_delta: StreamDeltaConsumer | None = None,
    ) -> ModelCallResponse:
        self._validate_prepared(call, prepared)
        started_at = self._now()
        in_flight = replace(call, status="in_flight", started_at=started_at)
        if not self._store.transition_model_call(
            model_call=in_flight,
            expected_status="prepared",
        ):
            raise ModelCallSendConflict("ModelCall in_flight CAS 失败，Provider 未调用")

        first_chunk_at: datetime | None = None

        async def consume(delta: StreamDelta, identity: ExecutionIdentity) -> None:
            nonlocal first_chunk_at
            if first_chunk_at is None and not isinstance(delta, StreamCompleted):
                first_chunk_at = self._now()
            if consume_delta is None:
                return
            value = consume_delta(delta, identity)
            if inspect.isawaitable(value):
                await value

        identity = ExecutionIdentity(
            session_id=call.session_id,
            operation_id=call.operation_id,
            step_id=call.step_id,
            step_sequence=call.step_sequence,
            model_call_id=call.model_call_id,
        )
        try:
            result = await effects.execute_prepared_model_call(
                prepared=prepared,
                identity=identity,
                consume_delta=consume,
                context_fingerprint=call.context_fingerprint,
            )
        except asyncio.CancelledError:
            cancelled = replace(
                in_flight,
                status="cancelled",
                first_chunk_at=first_chunk_at,
                finished_at=self._now(),
            )
            self._store.transition_model_call(
                model_call=cancelled,
                expected_status="in_flight",
            )
            raise
        except Exception as exc:
            raise ModelCallSendFailure(
                call=in_flight,
                cause=exc,
                first_chunk_at=first_chunk_at,
            ) from exc

        return ModelCallResponse(
            assistant_message=result.assistant_message,
            provider_response=result.provider_response,
            started_at=started_at,
            first_chunk_at=first_chunk_at,
            finished_at=self._now(),
            http_status=result.http_status,
        )

    @staticmethod
    def _validate_prepared(call: ModelCall, prepared: PreparedModelCall) -> None:
        if call.status != "prepared":
            raise ModelCallSendConflict("只有 prepared ModelCall 可以进入发送闸门")
        if (
            call.provider != prepared.provider
            or call.api_kind != prepared.api_kind
            or call.endpoint != prepared.endpoint
            or call.requested_model != prepared.requested_model
        ):
            raise ModelCallSendConflict(
                "PreparedModelCall 与持久化 ModelCall 身份不一致"
            )
