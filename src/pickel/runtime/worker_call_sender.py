"""Worker 模型调用的可靠发送：逐次落库、失败记账与有界退避重试。

历史压缩与 Goal 验证共用这一个发送实现，保证 worker 请求与主请求
遵守同一套 ModelCall 记账纪律；重试判定与退避取值复用
AgentRuntimePolicy 的策略方法，本模块不自带重试知识。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Protocol

from pickel.agents.agent_package import AgentRuntimePolicy
from pickel.conversations.agent_message import AssistantMessage
from pickel.context.model_context import ModelContext
from pickel.model_calls.service import ModelCallService
from pickel.providers.base import Provider
from pickel.providers.errors import classify_provider_error
from pickel.runtime.model_call_send_gate import ModelCallSendFailure, ModelCallSendGate
from pickel.runtime.runtime_effects import RuntimeEffects


class WorkerCallSendError(RuntimeError):
    """worker 模型调用在重试额度内仍未成功。"""


class WorkerSendEffect(Protocol):
    """注入 OperationDriver 的 worker 发送接缝；与 ToolEffect 同形的窄协议。"""

    async def __call__(
        self,
        *,
        session_id: str,
        context: ModelContext,
        purpose: str,
        worker_provider: Provider,
        runtime_policy: AgentRuntimePolicy,
        provider_timeout_seconds: float,
    ) -> AssistantMessage: ...


class WorkerCallSender:
    """历史压缩与 Goal 验证共用的可靠 worker 调用发送器。"""

    def __init__(
        self,
        *,
        model_calls: ModelCallService,
        send_gate: ModelCallSendGate,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self._model_calls = model_calls
        self._send_gate = send_gate
        self._sleep = sleep or asyncio.sleep

    async def __call__(
        self,
        *,
        session_id: str,
        context: ModelContext,
        purpose: str,
        worker_provider: Provider,
        runtime_policy: AgentRuntimePolicy,
        provider_timeout_seconds: float,
    ) -> AssistantMessage:
        if worker_provider is None:
            raise WorkerCallSendError("worker 模型调用需要配置 worker model")
        attempt = 1
        while True:
            prepare_async = getattr(
                self._model_calls, "prepare_session_call_async", None
            )
            if prepare_async is not None:
                prepared_call = await prepare_async(
                    session_id=session_id,
                    context=context,
                    mapper=worker_provider,
                    request_attempt=attempt,
                    model_role="worker",
                    purpose=purpose,
                )
            else:
                prepared_call = await asyncio.to_thread(
                    self._model_calls.prepare_session_call,
                    session_id=session_id,
                    context=context,
                    mapper=worker_provider,
                    request_attempt=attempt,
                    model_role="worker",
                    purpose=purpose,
                )
            effects = RuntimeEffects(
                provider=worker_provider,
                provider_name=prepared_call.prepared.provider,
                model_name=prepared_call.prepared.requested_model,
                provider_timeout_seconds=provider_timeout_seconds,
            )
            try:
                response = await self._send_gate.send(
                    call=prepared_call.model_call,
                    prepared=prepared_call.prepared,
                    effects=effects,
                )
            except ModelCallSendFailure as exc:
                error = classify_provider_error(exc.cause)
                record_failure_async = getattr(
                    self._model_calls, "record_send_failure_async", None
                )
                if record_failure_async is not None:
                    failed_call = await record_failure_async(
                        exc.call,
                        exc.cause,
                        first_chunk_at=exc.first_chunk_at,
                    )
                else:
                    failed_call = await asyncio.to_thread(
                        self._model_calls.record_send_failure,
                        exc.call,
                        exc.cause,
                        first_chunk_at=exc.first_chunk_at,
                    )
                if not runtime_policy.should_retry_worker_request(
                    retryable=error.retryable,
                    first_chunk_received=exc.first_chunk_at is not None,
                    completed_attempts=failed_call.request_attempt,
                ):
                    raise WorkerCallSendError(
                        f"worker 调用在 {failed_call.request_attempt} 次尝试后失败: {exc}"
                    ) from exc
                attempt = failed_call.request_attempt + 1
                await self._sleep(
                    runtime_policy.worker_retry_delay_ms(failed_call.request_attempt)
                    / 1000
                )
                continue
            complete_async = getattr(
                self._model_calls, "complete_session_response_async", None
            )
            if complete_async is not None:
                await complete_async(
                    call=prepared_call.model_call,
                    response=response,
                )
            else:
                await asyncio.to_thread(
                    self._model_calls.complete_session_response,
                    call=prepared_call.model_call,
                    response=response,
                )
            return response.assistant_message
