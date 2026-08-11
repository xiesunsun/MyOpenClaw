"""OperationDriver 允许跨越的统一副作用边界。"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections.abc import Awaitable, Callable
from contextlib import aclosing, nullcontext
from dataclasses import dataclass, replace
from typing import Any

from pickel.context.model_context import ModelContext
from pickel.conversations.agent_message import (
    AgentMessage,
    AssistantMessage,
    ModelResponseMetadata,
    ToolResultMessage,
)
from pickel.conversations.agent_message import UserMessage
from pickel.conversations.content_blocks import TextBlock
from pickel.operations.agent_run_state import AgentRunState
from pickel.operations.operation_service import (
    AgentRunProgressCommit,
    OperationService,
)
from pickel.observe.records import (
    ErrorInfo,
    ObservationIdentity,
    RequestSnapshotRecord,
    SpanTimer,
    observation_requested,
    record_request_snapshot,
)
from pickel.providers.stream import StreamCompleted, StreamDelta
from pickel.runtime.host_calls import HostCallClient
from pickel.runtime.runtime_bindings import RuntimeBindings
from pickel.runtime.tool_call_executor import ToolCallExecutor
from pickel.tools.base import ToolExecutionResult

StreamDeltaConsumer = Callable[[StreamDelta], None | Awaitable[None]]

logger = logging.getLogger(__name__)


class ModelExecutionBoundaryError(RuntimeError):
    pass


class EffectStateNotPersistedError(RuntimeError):
    pass


@dataclass(frozen=True)
class ModelRequestResult:
    assistant_message: AssistantMessage
    elapsed_ms: int
    first_delta_ms: float | None


class RuntimeEffects:
    """组合持久化、Provider 与工具副作用；不决定下一状态。"""

    def __init__(
        self,
        *,
        bindings: RuntimeBindings,
        operation_service: OperationService,
        tool_call_executor: ToolCallExecutor | None = None,
    ) -> None:
        self._bindings = bindings
        self._operation_service = operation_service
        self._tool_call_executor = tool_call_executor or ToolCallExecutor(bindings)

    def commit_operation_state(
        self,
        *,
        state: AgentRunState,
        appended_message: AssistantMessage | ToolResultMessage | None = None,
        appended_message_node_id: str | None = None,
    ) -> AgentRunProgressCommit:
        timer = SpanTimer("pickel.storage.commit", self._identity(state))
        try:
            committed = self._operation_service.commit_agent_run_state(
                state=state,
                appended_message=appended_message,
                appended_message_node_id=appended_message_node_id,
            )
        except Exception as exc:
            timer.finish(
                status="error",
                error=ErrorInfo.from_exception(exc, kind="storage"),
            )
            raise
        timer.finish(attributes={"revision": state.revision})
        return committed

    async def invoke_hook(self, hook_name: str, event: Any) -> Any:
        """Lifecycle Hook 的唯一执行边界。"""
        hook = getattr(self._bindings.lifecycle_hooks, hook_name, None)
        if hook is None:
            raise ValueError(f"未知 Lifecycle Hook: {hook_name}")
        result = hook(event)
        if inspect.isawaitable(result):
            return await result
        return result

    async def retrieve_recall_messages(
        self,
        *,
        session_id: str,
        visible_messages: list[AgentMessage],
    ) -> list[AgentMessage]:
        """在统一执行边界调用可选外部召回源；单源失败不阻断 AgentRun。"""
        current_user_text = self._latest_user_text(visible_messages)
        recalled: list[AgentMessage] = []
        for source in self._bindings.recall_sources:
            timer = SpanTimer(
                "pickel.recall.retrieve",
                ObservationIdentity(session_id=session_id),
                attributes={"source": type(source).__name__},
            )
            try:
                messages = await source.provide(
                    session_id=session_id,
                    current_user_text=current_user_text,
                )
                recalled.extend(messages)
                timer.finish(attributes={"message_count": len(messages)})
            except Exception as exc:
                timer.finish(
                    status="error",
                    error=ErrorInfo.from_exception(exc, kind="recall"),
                )
                logger.exception("Recall source %s failed", type(source).__name__)
        return recalled

    async def execute_model_request(
        self,
        *,
        state: AgentRunState,
        model_context: ModelContext,
        consume_delta: StreamDeltaConsumer | None = None,
        context_fingerprint: str | None = None,
        hook_injected_chars: int = 0,
    ) -> ModelRequestResult:
        self._require_persisted_state(state)
        step = state.current_step
        if step is None or step.phase != "model_request_intent_recorded":
            phase = step.phase if step is not None else "none"
            raise ModelExecutionBoundaryError(
                "Provider 请求必须先持久化 model_request_intent_recorded: " f"{phase}"
            )
        identity = self._identity(state)
        timer = SpanTimer(
            "pickel.provider.request",
            identity,
            attributes={
                "provider": self._bindings.agent_package_version.model.provider,
                "model": self._bindings.agent_package_version.model.model,
            },
        )
        if observation_requested("request_snapshot"):
            snapshot = self._bindings.provider.request_snapshot(model_context)
            if snapshot is not None:
                record_request_snapshot(
                    RequestSnapshotRecord(
                        provider=self._bindings.agent_package_version.model.provider,
                        model=self._bindings.agent_package_version.model.model,
                        request=snapshot,
                        identity=identity,
                    )
                )
        started = time.perf_counter()
        first_delta_ms: list[float | None] = [None]
        coroutine = self._consume_provider_stream(
            model_context=model_context,
            consume_delta=consume_delta,
            started=started,
            first_delta_ms=first_delta_ms,
        )
        try:
            assistant = await asyncio.wait_for(
                coroutine,
                timeout=self._bindings.provider_timeout_seconds,
            )
        except BaseException as exc:
            timer.finish(
                status=(
                    "cancelled" if isinstance(exc, asyncio.CancelledError) else "error"
                ),
                error=ErrorInfo.from_exception(exc, kind="provider"),
            )
            raise
        elapsed_ms = round((time.perf_counter() - started) * 1000)
        metadata = assistant.metadata or ModelResponseMetadata(
            provider=self._bindings.agent_package_version.model.provider,
            model=self._bindings.agent_package_version.model.model,
        )
        assistant = AssistantMessage(
            content=list(assistant.content),
            metadata=replace(
                metadata,
                elapsed_ms=(
                    metadata.elapsed_ms
                    if metadata.elapsed_ms is not None
                    else elapsed_ms
                ),
                context_fingerprint=context_fingerprint,
                hook_injected_chars=max(0, hook_injected_chars),
            ),
        )
        usage = assistant.metadata.usage if assistant.metadata is not None else None
        timer.finish(
            attributes={
                "ttft_ms": first_delta_ms[0],
                "input_tokens": usage.input_tokens if usage is not None else None,
                "output_tokens": usage.output_tokens if usage is not None else None,
                "cache_read_tokens": (
                    usage.cache_read_tokens if usage is not None else None
                ),
                "cache_write_tokens": (
                    usage.cache_write_tokens if usage is not None else None
                ),
            }
        )
        return ModelRequestResult(
            assistant_message=assistant,
            elapsed_ms=elapsed_ms,
            first_delta_ms=first_delta_ms[0],
        )

    async def execute_tool_call(
        self,
        *,
        state: AgentRunState,
        tool_call_id: str,
        host_calls: HostCallClient | None = None,
    ) -> ToolExecutionResult:
        self._require_persisted_state(state)
        step = state.current_step
        if step is None:
            raise ModelExecutionBoundaryError(
                "AgentRunState 没有可执行 ToolCall 的当前 ModelStep"
            )
        tool_call = next(
            (
                candidate
                for candidate in step.tool_calls
                if candidate.tool_call_id == tool_call_id
            ),
            None,
        )
        if tool_call is None:
            raise ModelExecutionBoundaryError(
                f"AgentRunState 不包含 ToolCall: {tool_call_id}"
            )
        operation = self._operation_service.load_session_operation(state.operation_id)
        timer = SpanTimer(
            "pickel.tool.execute",
            self._identity(state),
            attributes={
                "tool_call_id": tool_call.tool_call_id,
                "tool_name": tool_call.tool_name,
            },
        )
        try:
            result = await self._tool_call_executor.execute_tool_call(
                tool_call=tool_call,
                session_id=operation.session_id,
                operation_id=operation.operation_id,
                step_id=step.step_id,
                step_sequence=step.step_sequence,
                host_calls=host_calls,
            )
        except BaseException as exc:
            timer.finish(
                status=(
                    "cancelled" if isinstance(exc, asyncio.CancelledError) else "error"
                ),
                error=ErrorInfo.from_exception(exc, kind="tool"),
            )
            raise
        timer.finish(
            status=("error" if result.is_error else "ok"),
            attributes={"is_error": result.is_error},
            error=result.error,
        )
        return result

    def _require_persisted_state(self, state: AgentRunState) -> None:
        persisted = self._operation_service.load_agent_run_state(state.operation_id)
        if persisted != state:
            raise EffectStateNotPersistedError(
                "真实副作用只能从当前已持久化 AgentRunState 执行: "
                f"operation={state.operation_id}, revision={state.revision}"
            )

    def _identity(self, state: AgentRunState) -> ObservationIdentity:
        step = state.current_step
        operation = self._operation_service.load_session_operation(state.operation_id)
        return ObservationIdentity(
            session_id=operation.session_id,
            operation_id=state.operation_id,
            step_id=step.step_id if step is not None else None,
            step_sequence=step.step_sequence if step is not None else None,
        )

    async def _consume_provider_stream(
        self,
        *,
        model_context: ModelContext,
        consume_delta: StreamDeltaConsumer | None,
        started: float,
        first_delta_ms: list[float | None],
    ) -> AssistantMessage:
        iterator = self._bindings.provider.stream(model_context).__aiter__()
        closer = aclosing(iterator) if hasattr(iterator, "aclose") else nullcontext()
        async with closer:
            async for delta in iterator:
                if first_delta_ms[0] is None:
                    first_delta_ms[0] = round(
                        (time.perf_counter() - started) * 1000,
                        3,
                    )
                if consume_delta is not None:
                    consumed = consume_delta(delta)
                    if inspect.isawaitable(consumed):
                        await consumed
                if isinstance(delta, StreamCompleted):
                    return delta.message
        raise ValueError("Provider stream 未以 StreamCompleted 收尾")

    @staticmethod
    def _latest_user_text(messages: list[AgentMessage]) -> str:
        for message in reversed(messages):
            if not isinstance(message, UserMessage):
                continue
            parts = [
                block.text
                for block in message.content
                if isinstance(block, TextBlock) and block.text
            ]
            if parts:
                return "\n".join(parts)
        return ""
