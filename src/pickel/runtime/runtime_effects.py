"""RuntimeEffects：Provider、Tool、Hook 和 Recall 的窄副作用边界。"""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
from typing import Any, Protocol

from pickel.conversations.agent_message import (
    AgentMessage,
    AssistantMessage,
    ModelResponseMetadata,
    ToolResultMessage,
    UserMessage,
)
from pickel.conversations.content_blocks import TextBlock
from pickel.model_calls.prepared import PreparedModelCall
from pickel.operations.agent_run_state import AgentRunState
from pickel.operations.session_operation import SessionOperation
from pickel.providers.base import Provider
from pickel.providers.stream import StreamCompleted, StreamDelta, ToolCallArgsDelta
from pickel.shared.execution_identity import ExecutionIdentity
from pickel.shared.frozen_json import thaw_json

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


@dataclass(frozen=True)
class ModelRequestResult:
    assistant_message: AssistantMessage
    provider_response: dict[str, Any]
    elapsed_ms: int
    first_delta_ms: float | None
    http_status: int | None


@dataclass(frozen=True)
class RuntimeEffects:
    """外部副作用门面；模型生成只能消费已冻结 PreparedModelCall。"""

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

    async def execute_prepared_model_call(
        self,
        *,
        prepared: PreparedModelCall,
        identity: ExecutionIdentity,
        consume_delta: StreamDeltaConsumer | None = None,
        context_fingerprint: str | None = None,
        hook_injected_chars: int = 0,
    ) -> ModelRequestResult:
        """发送前的持久化/CAS 由 ModelCallSendGate 保证。"""
        started = time.perf_counter()
        first_delta: list[float | None] = [None]

        async def request() -> StreamCompleted:
            return await asyncio.wait_for(
                self._consume_prepared_stream(
                    prepared=prepared,
                    consume_delta=consume_delta,
                    identity=identity,
                    started=started,
                    first_delta=first_delta,
                ),
                timeout=self.provider_timeout_seconds,
            )

        if self.model_request_limiter is None:
            completed = await request()
        else:
            async with self.model_request_limiter:
                completed = await request()

        elapsed_ms = round((time.perf_counter() - started) * 1000)
        message = completed.message
        if message.metadata is not None:
            message = replace(
                message,
                metadata=replace(
                    message.metadata,
                    elapsed_ms=elapsed_ms,
                    context_fingerprint=context_fingerprint,
                    hook_injected_chars=max(0, hook_injected_chars),
                ),
            )
        else:
            provider_name = self.provider_name or prepared.provider
            model_name = self.model_name or prepared.requested_model
            message = replace(
                message,
                metadata=ModelResponseMetadata(
                    provider=provider_name,
                    model=model_name,
                    elapsed_ms=elapsed_ms,
                    context_fingerprint=context_fingerprint,
                    hook_injected_chars=max(0, hook_injected_chars),
                ),
            )
        response = thaw_json(completed.provider_response or {})
        if not isinstance(response, dict):
            raise TypeError("StreamCompleted.provider_response 必须是 JSON object")
        return ModelRequestResult(
            assistant_message=message,
            provider_response=response,
            elapsed_ms=elapsed_ms,
            first_delta_ms=first_delta[0],
            http_status=completed.http_status,
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

    async def _consume_prepared_stream(
        self,
        *,
        prepared: PreparedModelCall,
        consume_delta: StreamDeltaConsumer | None,
        identity: ExecutionIdentity,
        started: float,
        first_delta: list[float | None],
    ) -> StreamCompleted:
        async for delta in self.provider.stream_prepared(prepared):
            if first_delta[0] is None and not isinstance(delta, StreamCompleted):
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
                return delta
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
