"""RuntimeEffects：Provider、Tool、Hook 和 Recall 的窄副作用边界。

该模块不持有 Store、Package 注册表或 Runtime 资源袋。执行身份和恢复状态由
OperationDriver/OperationService 管理；本类只把已经冻结的输入交给外部实现。
"""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
from typing import Any, Protocol

from pickel.context.model_context import ModelContext
from pickel.conversations.agent_message import (
    AgentMessage,
    AssistantMessage,
    ModelResponseMetadata,
    ToolResultMessage,
    UserMessage,
)
from pickel.conversations.content_blocks import TextBlock
from pickel.operations.agent_run_state import AgentRunState
from pickel.operations.session_operation import SessionOperation
from pickel.observe.records import (
    RequestSnapshotRecord,
    observation_requested,
    record_request_snapshot,
)
from pickel.providers.base import Provider
from pickel.providers.stream import ToolCallArgsDelta, StreamCompleted, StreamDelta
from pickel.shared.execution_identity import ExecutionIdentity

StreamDeltaConsumer = Callable[[StreamDelta, ExecutionIdentity], None | Awaitable[None]]


class ToolEffect(Protocol):
    async def __call__(
        self,
        *,
        operation: SessionOperation,
        state: AgentRunState,
        tool_call_id: str,
        host_calls: Any | None = None,
    ) -> ToolResultMessage: ...


class HookEffect(Protocol):
    async def __call__(self, hook_name: str, event: Any) -> Any: ...


class RecallEffect(Protocol):
    async def provide(
        self, *, session_id: str, current_user_text: str = ""
    ) -> list[AgentMessage]: ...


class ModelExecutionBoundaryError(RuntimeError):
    """Provider 请求尚未满足 intent-before-effect 前置条件。"""


@dataclass(frozen=True)
class ModelRequestResult:
    assistant_message: AssistantMessage
    elapsed_ms: int
    first_delta_ms: float | None


@dataclass(frozen=True)
class RuntimeEffects:
    """外部副作用门面；每项能力都是显式依赖，不能从 RuntimeStore 取。"""

    provider: Provider
    execute_tool: ToolEffect | None = None
    invoke_hook_effect: HookEffect | None = None
    recall_sources: tuple[RecallEffect, ...] = ()
    provider_timeout_seconds: float = 600.0
    provider_name: str = ""
    model_name: str = ""
    model_request_limiter: asyncio.Semaphore | None = None

    async def invoke_hook(self, hook_name: str, event: Any) -> Any:
        if self.invoke_hook_effect is None:
            return None
        result = self.invoke_hook_effect(hook_name, event)
        if inspect.isawaitable(result):
            return await result
        return result

    async def retrieve_recall_messages(
        self,
        *,
        session_id: str,
        visible_messages: Sequence[AgentMessage],
    ) -> list[AgentMessage]:
        current_user_text = _latest_user_text(visible_messages)
        messages: list[AgentMessage] = []
        for source in self.recall_sources:
            result = await source.provide(
                session_id=session_id,
                current_user_text=current_user_text,
            )
            messages.extend(result)
        return messages

    async def execute_model_request(
        self,
        *,
        operation: SessionOperation,
        state: AgentRunState,
        model_context: ModelContext,
        consume_delta: StreamDeltaConsumer | None = None,
        context_fingerprint: str | None = None,
        hook_injected_chars: int = 0,
    ) -> ModelRequestResult:
        if operation.operation_id != state.operation_id:
            raise ModelExecutionBoundaryError("Operation 与 AgentRunState 身份不一致")
        step = state.current_step
        if step is None or step.phase != "request_ready":
            phase = step.phase if step is not None else "none"
            raise ModelExecutionBoundaryError(
                "Provider 请求必须先持久化 request_ready intent: " f"{phase}"
            )
        self._record_request_snapshot(
            operation=operation,
            state=state,
            model_context=model_context,
        )
        identity = ExecutionIdentity(
            session_id=operation.session_id,
            operation_id=operation.operation_id,
            step_id=step.step_id,
            step_sequence=step.step_sequence,
        )
        started = time.perf_counter()
        first_delta: list[float | None] = [None]

        async def request() -> AssistantMessage:
            return await asyncio.wait_for(
                self._consume_stream(
                    model_context=model_context,
                    consume_delta=consume_delta,
                    identity=identity,
                    started=started,
                    first_delta=first_delta,
                ),
                timeout=self.provider_timeout_seconds,
            )

        if self.model_request_limiter is None:
            message = await request()
        else:
            async with self.model_request_limiter:
                message = await request()
        elapsed_ms = round((time.perf_counter() - started) * 1000)
        if message.metadata is not None:
            metadata = replace(
                message.metadata,
                # Runtime 实测耗时是后续 Usage 聚合的原始事实，覆盖 Provider
                # 可能附带的 elapsed_ms；其余 Provider 响应字段保持不变。
                elapsed_ms=elapsed_ms,
                context_fingerprint=context_fingerprint,
                hook_injected_chars=max(0, hook_injected_chars),
            )
            message = replace(message, metadata=metadata)
        elif self.provider_name and self.model_name:
            # Provider 没有返回 metadata 时，只有两个稳定身份都存在才创建
            # Runtime metadata；不能用空字符串伪造 Provider/model 身份。
            message = replace(
                message,
                metadata=ModelResponseMetadata(
                    provider=self.provider_name,
                    model=self.model_name,
                    elapsed_ms=elapsed_ms,
                    context_fingerprint=context_fingerprint,
                    hook_injected_chars=max(0, hook_injected_chars),
                ),
            )
        return ModelRequestResult(
            assistant_message=message,
            elapsed_ms=elapsed_ms,
            first_delta_ms=first_delta[0],
        )

    def _record_request_snapshot(
        self,
        *,
        operation: SessionOperation,
        state: AgentRunState,
        model_context: ModelContext,
    ) -> None:
        """只在 Observer 明确请求时构建快照，观测失败不影响执行。"""
        if not observation_requested("request_snapshot"):
            return
        try:
            snapshot = self.provider.request_snapshot(model_context)
        except Exception:
            return
        if snapshot is None:
            return

        step = state.current_step
        record_request_snapshot(
            RequestSnapshotRecord(
                provider=self.provider_name,
                model=self.model_name,
                request=snapshot,
                cache_order=tuple(self.provider.request_cache_order),
                identity=ExecutionIdentity(
                    session_id=operation.session_id,
                    operation_id=operation.operation_id,
                    step_id=step.step_id if step is not None else None,
                    step_sequence=step.step_sequence if step is not None else None,
                ),
            )
        )

    async def execute_tool_call(
        self,
        *,
        operation: SessionOperation,
        state: AgentRunState,
        tool_call_id: str,
        host_calls: Any | None = None,
    ) -> ToolResultMessage:
        if self.execute_tool is None:
            raise RuntimeError("未配置 ToolEffect")
        if operation.operation_id != state.operation_id:
            raise RuntimeError("Operation 与 AgentRunState 身份不一致")
        step = state.current_step
        call = (
            next(
                (item for item in step.tool_calls if item.tool_call_id == tool_call_id),
                None,
            )
            if step is not None and step.phase == "awaiting_tools"
            else None
        )
        if call is None or call.status != "intent_recorded":
            raise RuntimeError(
                "Tool 调用必须先持久化 intent_recorded: " f"{tool_call_id}"
            )
        return await self.execute_tool(
            operation=operation,
            state=state,
            tool_call_id=tool_call_id,
            host_calls=host_calls,
        )

    async def _consume_stream(
        self,
        *,
        model_context: ModelContext,
        consume_delta: StreamDeltaConsumer | None,
        identity: ExecutionIdentity,
        started: float,
        first_delta: list[float | None],
    ) -> AssistantMessage:
        async for delta in self.provider.stream(model_context):
            if first_delta[0] is None:
                first_delta[0] = round((time.perf_counter() - started) * 1000, 3)
            if consume_delta is not None:
                delta_identity = replace(
                    identity,
                    tool_call_id=(
                        delta.tool_call_id
                        if isinstance(delta, ToolCallArgsDelta)
                        else None
                    ),
                )
                consumed = consume_delta(delta, delta_identity)
                if inspect.isawaitable(consumed):
                    await consumed
            if isinstance(delta, StreamCompleted):
                return delta.message
        raise ValueError("Provider stream 未以 StreamCompleted 收尾")


def _latest_user_text(messages: Sequence[AgentMessage]) -> str:
    for message in reversed(messages):
        if isinstance(message, UserMessage):
            text = [
                block.text
                for block in message.content
                if isinstance(block, TextBlock) and block.text
            ]
            if text:
                return "\n".join(text)
    return ""
