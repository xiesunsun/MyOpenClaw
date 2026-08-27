"""OperationDriver：推进一个已接受的 SessionOperation。

接受 Inbox、恢复 active Operation 属于 AgentDriver；本类只消费一个已有
Operation，并严格执行 ``intent commit -> 外部副作用 -> 结果 commit``。
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from uuid import uuid4

from pickel.agents.agent_package import AgentPackageVersion
from pickel.agents.agent_package_loader import PackageLoadError
from pickel.context.model_context import ModelContext
from pickel.context.model_context_builder import (
    ContextContributions,
    ModelContextBuilder,
)
from pickel.context.projection import ConversationProjector
from pickel.context.window import apply_window
from pickel.conversations.agent_message import (
    AgentMessage,
    AssistantMessage,
    ToolResultMessage,
)
from pickel.conversations.content_blocks import TextBlock, ToolCallBlock
from pickel.conversations.conversation_service import ConversationService
from pickel.conversations.conversation_node import ConversationNode
from pickel.hooks.decisions import PreToolUseDecision
from pickel.hooks.events import BeforeRequestEvent, PreToolUseEvent
from pickel.inbox.message import InboxMessage
from pickel.operations.agent_run_state import (
    AgentRunError,
    AgentRunState,
    DelegateAgentIntent,
    ModelRequestIntent,
    ModelStepState,
    ToolApproval,
    ToolCallState,
    ToolReplayPolicy,
)
from pickel.operations.session_operation import SessionOperation
from pickel.operations.operation_service import OperationService
from pickel.model_calls.service import (
    ModelCallPrepareConflict,
    ModelCallRecoveryError,
    ModelCallRetryExhausted,
    ModelCallService,
)
from pickel.runtime.model_call_send_gate import (
    ModelCallSendConflict,
    ModelCallSendFailure,
    ModelCallSendGate,
)
from pickel.providers.stream import StreamDelta
from pickel.providers.errors import ProviderRequestError, classify_provider_error
from pickel.runtime.agent_run_usage import AgentRunUsage, project_agent_run_usage
from pickel.runtime.runtime_effects import RuntimeEffects
from pickel.runtime.runtime_events import (
    RuntimeEventBase,
    ToolCallCompleted,
    ToolCallStarted,
)
from pickel.shared.event_envelope import EventEnvelope
from pickel.shared.frozen_json import freeze_json_object, thaw_json
from pickel.shared.execution_identity import ExecutionIdentity
from pickel.tools.validation import validate_json_schema

StreamDeltaConsumer = Callable[[StreamDelta, ExecutionIdentity], None | Awaitable[None]]
RuntimeEventConsumer = Callable[[RuntimeEventBase], None | Awaitable[None]]
DelegationPackageResolver = Callable[
    [SessionOperation, AgentPackageVersion, str], AgentPackageVersion
]

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OperationDriveResult:
    operation_id: str
    status: str
    state: AgentRunState
    assistant_message: AssistantMessage | None = None
    usage: AgentRunUsage | None = None


_AUTO_USAGE_LEAF = object()


async def _consume_runtime_event(
    consumer: RuntimeEventConsumer | None,
    event: RuntimeEventBase,
) -> None:
    if consumer is None:
        return
    consumed = consumer(event)
    if inspect.isawaitable(consumed):
        await consumed


class OperationDriver:
    """唯一的 Agent Tool Loop；不接受新消息，不拥有 Store。"""

    def __init__(
        self,
        *,
        operation_service: OperationService,
        conversation_service: ConversationService,
        package_loader: Callable[[SessionOperation], AgentPackageVersion],
        effects_resolver: Callable[[SessionOperation], RuntimeEffects],
        model_call_service: ModelCallService | None = None,
        release_operation_package: (
            Callable[[SessionOperation], Awaitable[None]] | None
        ) = None,
        model_context_builder: ModelContextBuilder | None = None,
        step_id_factory: Callable[[], str] | None = None,
        node_id_factory: Callable[[], str] | None = None,
        now: Callable[[], datetime] | None = None,
        wake_callback: Callable[[str], None] | None = None,
        terminal_callback: Callable[[str], None] | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        delegation_package_resolver: DelegationPackageResolver | None = None,
        allowed_tool_names: frozenset[str] | None = None,
    ) -> None:
        self._operations = operation_service
        self._conversations = conversation_service
        self._package_loader = package_loader
        self._effects_resolver = effects_resolver
        if model_call_service is None:
            candidate = getattr(operation_service, "_store", None)
            if candidate is not None and hasattr(candidate, "model_call_content_store"):
                model_call_service = ModelCallService(candidate)
        self._model_calls = model_call_service
        self._release_operation_package = release_operation_package
        self._context_builder = model_context_builder or ModelContextBuilder()
        self._step_id = step_id_factory or (lambda: str(uuid4()))
        self._node_id = node_id_factory or (lambda: str(uuid4()))
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._wake_callback = wake_callback
        self._terminal_callback = terminal_callback
        self._sleep = sleep or asyncio.sleep
        self._delegation_package_resolver = delegation_package_resolver
        self._allowed_tool_names = allowed_tool_names

    async def drive_operation(
        self,
        operation_id: str,
        *,
        consume_delta: StreamDeltaConsumer | None = None,
        consume_tool_event: RuntimeEventConsumer | None = None,
        host_calls=None,
    ) -> OperationDriveResult:
        result = await self._drive_operation(
            operation_id,
            consume_delta=consume_delta,
            consume_tool_event=consume_tool_event,
            host_calls=host_calls,
        )
        if result.status in {"succeeded", "failed", "cancelled"}:
            self._wake_parent(operation_id)
        if (
            result.status in {"succeeded", "failed", "cancelled"}
            and self._release_operation_package is not None
        ):
            operation = self._operations.load_operation(operation_id)
            try:
                await self._release_operation_package(operation)
            except Exception:
                # 业务终态已经提交；生命周期清理失败只能诊断，不能反转结果。
                logger.exception(
                    "释放 Operation Package 引用失败: operation_id=%s",
                    operation_id,
                )
        return result

    async def _drive_operation(
        self,
        operation_id: str,
        *,
        consume_delta: StreamDeltaConsumer | None = None,
        consume_tool_event: RuntimeEventConsumer | None = None,
        host_calls=None,
    ) -> OperationDriveResult:
        operation = self._operations.load_operation(operation_id)
        state = self._operations.load_agent_run_state(operation_id)
        if state.status in {"succeeded", "failed", "cancelled"}:
            return self._result(
                operation,
                state,
                usage_leaf=(_AUTO_USAGE_LEAF if state.status == "succeeded" else None),
            )
        try:
            package = self._package_loader(operation)
            effects = self._effects_resolver(operation)
        except PackageLoadError as exc:
            usage_leaf = self._current_reliable_leaf(operation)
            failed = replace(
                state,
                status="failed",
                current_step=None,
                error=AgentRunError(
                    code=exc.code,
                    message=str(exc),
                    retryable=True,
                ),
            )
            state = self._commit(failed, state)
            return self._result(
                operation,
                state,
                usage_leaf=usage_leaf,
            )
        if package.package_version_id != operation.agent_package_version_id:
            raise RuntimeError("Package Loader 返回了错误的 AgentPackageVersion")
        last_assistant: AssistantMessage | None = None

        while True:
            if state.status in {"succeeded", "failed", "cancelled"}:
                return self._result(operation, state, last_assistant)
            if state.status == "waiting":
                return self._result(operation, state, last_assistant)

            if state.status == "cancelling":
                wake_sessions = self._operations.reconcile_cancellation(
                    operation_id,
                    reason=(
                        state.cancellation.cause
                        if state.cancellation is not None
                        else None
                    ),
                )
                self._wake_sessions(wake_sessions)
                refreshed = self._operations.load_agent_run_state(operation_id)
                if refreshed.revision != state.revision:
                    state = refreshed
                    continue
                step = state.current_step
                if step is not None and any(
                    call.status == "intent_recorded" and call.replay_policy == "never"
                    for call in step.tool_calls
                ):
                    return self._result(operation, state, last_assistant)
                if not self._operations.cancellation_ready(operation_id):
                    return self._result(operation, state, last_assistant)
                usage_leaf = self._current_reliable_leaf(operation)
                cancelled = replace(state, status="cancelled", current_step=None)
                if not self._operations.commit_transition(
                    state=replace(cancelled, revision=state.revision + 1),
                    expected_revision=state.revision,
                    node=None,
                    updated_at=self._now(),
                ):
                    refreshed = self._operations.load_agent_run_state(operation_id)
                    if refreshed.revision != state.revision:
                        state = refreshed
                        continue
                    return self._result(operation, state, last_assistant)
                state = replace(cancelled, revision=state.revision + 1)
                return self._result(
                    operation,
                    state,
                    last_assistant,
                    usage_leaf=usage_leaf,
                )

            if state.status == "queued":
                state = self._commit(replace(state, status="running"), state)
                continue

            step = state.current_step
            if step is None:
                if state.completed_step_count >= package.runtime_policy.max_model_steps:
                    usage_leaf = self._current_reliable_leaf(operation)
                    state = self._commit(
                        replace(
                            state,
                            status="failed",
                            error=AgentRunError(
                                code="max_model_steps_exceeded",
                                message="AgentRun 已达到最大模型 Step 数",
                                retryable=False,
                            ),
                        ),
                        state,
                    )
                    return self._result(
                        operation,
                        state,
                        last_assistant,
                        usage_leaf=usage_leaf,
                    )
                step = ModelStepState(
                    step_id=self._step_id(),
                    step_sequence=state.completed_step_count + 1,
                    phase="preparing_request",
                    request_attempt=0,
                    request_intent=None,
                    assistant_message_node_id=None,
                    tool_calls=(),
                )
                candidate = replace(
                    state,
                    current_step=step,
                    status="running",
                    revision=state.revision + 1,
                )
                if self._pending_step_messages(operation.session_id):
                    if not self._claim_step_messages(
                        operation=operation,
                        previous=state,
                        candidate=candidate,
                    ):
                        state = self._commit(candidate, state)
                    else:
                        state = candidate
                else:
                    state = self._commit(candidate, state)
                continue

            if step.phase == "preparing_request":
                # Context 构建前反复读取并批量 claim，确保这次请求看到完整快照。
                pending = self._pending_step_messages(operation.session_id)
                if pending:
                    candidate = replace(state, revision=state.revision + 1)
                    if self._claim_step_messages(
                        operation=operation,
                        previous=state,
                        candidate=candidate,
                    ):
                        state = candidate
                        continue
                    refreshed = self._operations.load_agent_run_state(
                        operation.operation_id
                    )
                    if refreshed.revision != state.revision:
                        raise RuntimeError(
                            "AgentRunState CAS 冲突，停止推进且不重放副作用"
                        )
                    if self._pending_step_messages(operation.session_id):
                        raise RuntimeError("Step 消息 claim 冲突，停止推进")
                context = await self._build_context(
                    operation=operation,
                    state=state,
                    package=package,
                    effects=effects,
                )
                intent = ModelRequestIntent(
                    model_context=context,
                    context_fingerprint=_fingerprint(context),
                )
                next_step = replace(
                    step,
                    phase="request_ready",
                    request_intent=intent,
                )
                next_state = replace(
                    state,
                    revision=state.revision + 1,
                    current_step=next_step,
                )
                if not self._operations.commit_transition(
                    state=next_state,
                    expected_revision=state.revision,
                    node=None,
                ):
                    refreshed = self._operations.load_agent_run_state(
                        operation.operation_id
                    )
                    if (
                        refreshed.revision == state.revision
                        and self._pending_step_messages(operation.session_id)
                    ):
                        state = refreshed
                        continue
                    raise RuntimeError("AgentRunState CAS 冲突，停止推进且不重放副作用")
                state = next_state
                continue

            if step.phase == "request_ready":
                if self._model_calls is None:
                    raise RuntimeError("OperationDriver 未配置 ModelCallService")
                assert step.request_intent is not None
                max_attempts = getattr(
                    package.runtime_policy, "model_request_max_attempts", 3
                )
                try:
                    prepared_call = self._model_calls.prepare_or_recover_agent_call(
                        operation=operation,
                        state=state,
                        mapper=effects.provider,
                        max_attempts=max_attempts,
                    )
                except ModelCallRetryExhausted as exc:
                    call_error = exc.call.error if exc.call is not None else None
                    failed = replace(
                        state,
                        status="failed",
                        current_step=None,
                        error=AgentRunError(
                            code=(
                                call_error.code
                                if call_error is not None
                                else "provider_retry_exhausted"
                            ),
                            message=(
                                call_error.message
                                if call_error is not None
                                else str(exc)
                            ),
                            retryable=(
                                bool(call_error.retryable)
                                if call_error is not None
                                else True
                            ),
                        ),
                    )
                    state = self._commit(failed, state)
                    return self._result(
                        operation,
                        state,
                        usage_leaf=self._current_reliable_leaf(operation),
                    )
                except (ModelCallPrepareConflict, ModelCallRecoveryError) as exc:
                    raise RuntimeError(str(exc)) from exc

                state = prepared_call.state
                step = state.current_step
                assert step is not None and step.request_intent is not None
                retry_after = prepared_call.retry_after_attempt
                if retry_after is not None:
                    delay_ms = min(
                        getattr(
                            package.runtime_policy,
                            "model_request_retry_initial_delay_ms",
                            1000,
                        )
                        * (2 ** (retry_after - 1)),
                        getattr(
                            package.runtime_policy,
                            "model_request_retry_max_delay_ms",
                            4000,
                        ),
                    )
                    await self._sleep(delay_ms / 1000)

                gate = ModelCallSendGate(
                    self._model_calls.store,
                    now=self._now,
                )
                try:
                    response = await gate.send(
                        call=prepared_call.model_call,
                        prepared=prepared_call.prepared,
                        effects=effects,
                        consume_delta=consume_delta,
                    )
                except ModelCallSendFailure as exc:
                    error = classify_provider_error(exc.cause)
                    failed_call = self._model_calls.mark_failed(
                        exc.call,
                        error,
                        first_chunk_at=exc.first_chunk_at,
                    )
                    if error.retryable and failed_call.request_attempt < max_attempts:
                        continue
                    failed = replace(
                        state,
                        status="failed",
                        current_step=None,
                        error=AgentRunError(
                            code=error.code,
                            message=str(error),
                            retryable=error.retryable,
                        ),
                    )
                    state = self._commit(failed, state)
                    return self._result(
                        operation,
                        state,
                        usage_leaf=self._current_reliable_leaf(operation),
                    )
                except ModelCallSendConflict as exc:
                    raise RuntimeError(str(exc)) from exc

                response_content_ref = None
                save_response_content = getattr(
                    self._model_calls, "save_response_content", None
                )
                if save_response_content is not None:
                    response_content_ref = save_response_content(response)

                assistant_node_id = self._node_id()
                try:
                    tool_calls = await self._prepare_tool_calls(
                        response.assistant_message,
                        operation=operation,
                        state=state,
                        package=package,
                        effects=effects,
                    )
                except Exception as exc:
                    # Provider 已经返回了完整结果；Hook/工具策略处理失败时，
                    # 仍把结果和失败状态作为一个可靠事实提交，避免留下 in_flight。
                    logger.exception(
                        "处理模型响应失败，收敛 Operation: operation_id=%s",
                        operation.operation_id,
                    )
                    failure = classify_provider_error(exc)
                    session = self._conversations.load_conversation_session(
                        operation.session_id
                    )
                    node = ConversationNode(
                        node_id=assistant_node_id,
                        session_id=operation.session_id,
                        parent_node_id=session.active_node_id,
                        content_type="agent_message",
                        content=response.assistant_message,
                        created_at=response.finished_at,
                    )
                    failed_state = replace(
                        state,
                        revision=state.revision + 1,
                        status="failed",
                        current_step=None,
                        final_assistant_node_id=None,
                        error=AgentRunError(
                            code="model_response_processing_failed",
                            message=f"模型响应处理失败: {exc}",
                            retryable=False,
                        ),
                    )
                    failure = ProviderRequestError(
                        code="model_response_processing_failed",
                        message=f"模型响应处理失败: {exc}",
                        retryable=False,
                        status_code=failure.status_code,
                    )
                    self._model_calls.commit_agent_processing_failure(
                        call=prepared_call.model_call,
                        response=response,
                        state=failed_state,
                        expected_revision=state.revision,
                        node=node,
                        error=failure,
                        response_content_ref=response_content_ref,
                    )
                    state = failed_state
                    return self._result(
                        operation,
                        state,
                        response.assistant_message,
                        usage_leaf=self._current_reliable_leaf(operation),
                    )
                has_waiting_approval = any(
                    call.status == "waiting_approval" for call in tool_calls
                )
                next_step = replace(
                    step,
                    phase="awaiting_tools",
                    request_intent=None,
                    assistant_message_node_id=assistant_node_id,
                    tool_calls=tool_calls,
                )
                next_state = replace(
                    state,
                    revision=state.revision + 1,
                    status="waiting" if has_waiting_approval else "running",
                    waiting_reason=("tool_approval" if has_waiting_approval else None),
                    current_step=next_step,
                )
                session = self._conversations.load_conversation_session(
                    operation.session_id
                )
                node = ConversationNode(
                    node_id=assistant_node_id,
                    session_id=operation.session_id,
                    parent_node_id=session.active_node_id,
                    content_type="agent_message",
                    content=response.assistant_message,
                    created_at=response.finished_at,
                )
                commit_kwargs = {
                    "call": prepared_call.model_call,
                    "response": response,
                    "state": next_state,
                    "expected_revision": state.revision,
                    "node": node,
                }
                if response_content_ref is not None:
                    commit_kwargs["response_content_ref"] = response_content_ref
                self._model_calls.commit_agent_response(**commit_kwargs)
                state = next_state
                last_assistant = response.assistant_message
                continue

            if step.phase == "awaiting_tools" and not step.tool_calls:
                final_node_id = step.assistant_message_node_id
                assert final_node_id is not None
                next_state = replace(
                    state,
                    revision=state.revision + 1,
                    current_step=None,
                    completed_step_count=state.completed_step_count + 1,
                    status="succeeded",
                    final_assistant_node_id=final_node_id,
                )
                if self._operations.commit_transition(
                    state=next_state,
                    expected_revision=state.revision,
                    node=None,
                ):
                    state = next_state
                    return self._result(operation, state, last_assistant)
                refreshed = self._operations.load_agent_run_state(
                    operation.operation_id
                )
                if refreshed.revision != state.revision:
                    raise RuntimeError("AgentRunState CAS 冲突，停止推进且不重放副作用")
                if not self._pending_step_messages(operation.session_id):
                    raise RuntimeError("AgentRunState CAS 冲突，停止推进且不重放副作用")
                continue_state = replace(
                    refreshed,
                    revision=refreshed.revision + 1,
                    current_step=None,
                    completed_step_count=refreshed.completed_step_count + 1,
                    status="running",
                    waiting_reason=None,
                )
                if not self._operations.commit_transition(
                    state=continue_state,
                    expected_revision=refreshed.revision,
                    node=None,
                ):
                    raise RuntimeError("AgentRunState CAS 冲突，停止推进且不重放副作用")
                state = continue_state
                continue

            if step.phase != "awaiting_tools":
                raise RuntimeError(f"未知 ModelStep phase: {step.phase}")

            pending = next(
                (call for call in step.tool_calls if call.status != "completed"), None
            )
            if pending is not None and pending.status == "rejected":
                result_node_id = self._node_id()
                completed = replace(
                    pending,
                    status="completed",
                    result_node_id=result_node_id,
                    is_error=True,
                )
                calls = tuple(
                    (completed if call.tool_call_id == pending.tool_call_id else call)
                    for call in step.tool_calls
                )
                decision = pending.approval.decision if pending.approval else None
                reason = (
                    decision.reason if decision is not None else pending.decision_reason
                )
                content = "工具调用被拒绝"
                if reason:
                    content = f"{content}：{reason}"
                state = self._commit(
                    replace(
                        state,
                        current_step=replace(step, tool_calls=calls),
                    ),
                    state,
                    message=ToolResultMessage(
                        tool_call_id=pending.tool_call_id,
                        tool_name=pending.tool_name,
                        content=(TextBlock(text=content),),
                        is_error=True,
                    ),
                    node_id=result_node_id,
                )
                continue

            ready = (
                pending if pending is not None and pending.status == "ready" else None
            )
            executable: ToolCallState | None = None
            if ready is not None:
                # 保留 Provider 原始顺序，不能用只含当前调用的列表覆盖批次。
                intent_error: Exception | None = None
                execution_intent = ready.execution_intent
                if ready.tool_name == "delegate_agent":
                    try:
                        execution_intent = self._resolve_delegation_intent(
                            operation=operation,
                            package=package,
                            call=ready,
                        )
                    except Exception as exc:
                        intent_error = exc
                next_calls = tuple(
                    (
                        (
                            replace(
                                call,
                                status="rejected",
                                execution_intent=None,
                                decision_reason=f"delegate_agent 目标不可用: {intent_error}",
                            )
                            if intent_error is not None
                            else replace(
                                call,
                                status="intent_recorded",
                                execution_intent=execution_intent,
                            )
                        )
                        if call.tool_call_id == ready.tool_call_id
                        else call
                    )
                    for call in step.tool_calls
                )
                state = self._commit(
                    replace(state, current_step=replace(step, tool_calls=next_calls)),
                    state,
                )
                if intent_error is not None:
                    continue
                assert state.current_step is not None
                executable = next(
                    call
                    for call in state.current_step.tool_calls
                    if call.tool_call_id == ready.tool_call_id
                )
            else:
                recorded = (
                    pending
                    if pending is not None and pending.status == "intent_recorded"
                    else None
                )
                if recorded is not None and recorded.replay_policy == "never":
                    state = self._commit(
                        replace(
                            state,
                            status="waiting",
                            waiting_reason="tool_reconciliation",
                        ),
                        state,
                    )
                    return self._result(operation, state, last_assistant)
                executable = recorded

            if executable is not None:
                identity = ExecutionIdentity(
                    session_id=operation.session_id,
                    operation_id=operation.operation_id,
                    step_id=state.current_step.step_id,
                    step_sequence=state.current_step.step_sequence,
                    tool_call_id=executable.tool_call_id,
                )
                await _consume_runtime_event(
                    consume_tool_event,
                    ToolCallStarted(
                        envelope=EventEnvelope(identity=identity),
                        tool_name=executable.tool_name,
                        arguments=thaw_json(executable.arguments),
                    ),
                )
                result = await effects.execute_tool_call(
                    operation=operation,
                    state=state,
                    tool_call_id=executable.tool_call_id,
                    host_calls=host_calls,
                )
                if not isinstance(result, ToolResultMessage):
                    raise RuntimeError("ToolEffect 必须返回已渲染 ToolResultMessage")
                if (
                    result.tool_call_id != executable.tool_call_id
                    or result.tool_name != executable.tool_name
                ):
                    raise RuntimeError("ToolResultMessage 与 ToolCall 身份不一致")
                result_node_id = self._node_id()
                completed = replace(
                    next(
                        call
                        for call in state.current_step.tool_calls
                        if call.tool_call_id == executable.tool_call_id
                    ),
                    status="completed",
                    result_node_id=result_node_id,
                    is_error=result.is_error,
                )
                calls = tuple(
                    (
                        completed
                        if call.tool_call_id == executable.tool_call_id
                        else call
                    )
                    for call in state.current_step.tool_calls
                )
                next_step = replace(state.current_step, tool_calls=calls)
                message = result
                state = self._commit(
                    replace(state, current_step=next_step),
                    state,
                    message=message,
                    node_id=result_node_id,
                )
                await _consume_runtime_event(
                    consume_tool_event,
                    ToolCallCompleted(
                        envelope=EventEnvelope(identity=identity),
                        tool_name=executable.tool_name,
                        content=_tool_result_text(result),
                        is_error=result.is_error,
                    ),
                )
                continue

            if all(call.status == "completed" for call in step.tool_calls):
                state = self._commit(
                    replace(
                        state,
                        current_step=None,
                        completed_step_count=state.completed_step_count + 1,
                        status="running",
                        waiting_reason=None,
                    ),
                    state,
                )
                continue
            raise RuntimeError("ToolCallState 存在无法推进的状态")

    async def _prepare_tool_calls(
        self,
        message: AssistantMessage,
        *,
        operation: SessionOperation,
        state: AgentRunState,
        package: AgentPackageVersion,
        effects: RuntimeEffects,
    ) -> tuple[ToolCallState, ...]:
        """在 AssistantMessage 提交前冻结 PreToolUse 的最终决定。"""
        step = state.current_step
        if step is None or step.phase != "request_ready":
            raise RuntimeError("PreToolUse 必须发生在 request_ready ModelStep")
        tools = {tool.name: tool for tool in package.tools}
        if self._allowed_tool_names is not None:
            tools = {
                name: tool
                for name, tool in tools.items()
                if name in self._allowed_tool_names
            }
        calls: list[ToolCallState] = []
        for block in message.content:
            if not isinstance(block, ToolCallBlock):
                continue
            tool = tools.get(block.name)
            if tool is None:
                calls.append(
                    _rejected_tool_call(
                        block,
                        arguments=dict(block.arguments),
                        reason=f"工具不可用: {block.name}",
                    )
                )
                continue

            decision = await effects.invoke_hook(
                "pre_tool_use",
                PreToolUseEvent(
                    identity=ExecutionIdentity(
                        session_id=operation.session_id,
                        operation_id=operation.operation_id,
                        step_id=step.step_id,
                        step_sequence=step.step_sequence,
                        tool_call_id=block.id,
                    ),
                    tool_name=block.name,
                    arguments=dict(block.arguments),
                    tool_source=tool.source.value,
                    tool_origin=tool.implementation_ref.name,
                ),
            )
            if not isinstance(decision, PreToolUseDecision):
                decision = PreToolUseDecision()
            try:
                arguments = (
                    dict(decision.updated_arguments)
                    if decision.updated_arguments is not None
                    else dict(block.arguments)
                )
                freeze_json_object(arguments)
            except (TypeError, ValueError):
                calls.append(
                    _rejected_tool_call(
                        block,
                        arguments=dict(block.arguments),
                        reason="PreToolUse Hook 返回了无效参数",
                    )
                )
                continue

            if decision.action == "deny":
                calls.append(
                    _rejected_tool_call(
                        block,
                        arguments=arguments,
                        reason=decision.reason or "工具调用被 Hook 拒绝",
                        replay_policy=tool.replay_policy,
                    )
                )
                continue
            validation_error = validate_json_schema(arguments, tool.input_schema)
            if validation_error is not None:
                calls.append(
                    _rejected_tool_call(
                        block,
                        arguments=arguments,
                        reason=f"工具参数无效: {validation_error}",
                        replay_policy=tool.replay_policy,
                    )
                )
                continue
            if decision.action == "ask":
                calls.append(
                    ToolCallState(
                        tool_call_id=block.id,
                        tool_name=block.name,
                        arguments=arguments,
                        status="waiting_approval",
                        approval=ToolApproval(
                            requested_at=self._now(),
                            requested_by="hook",
                            reason=decision.reason,
                            decision=None,
                        ),
                        replay_policy=tool.replay_policy,
                        execution_intent=None,
                        decision_reason=None,
                        result_node_id=None,
                        is_error=None,
                    )
                )
                continue
            calls.append(
                ToolCallState(
                    tool_call_id=block.id,
                    tool_name=block.name,
                    arguments=arguments,
                    status="ready",
                    approval=None,
                    replay_policy=tool.replay_policy,
                    execution_intent=None,
                    decision_reason=None,
                    result_node_id=None,
                    is_error=None,
                )
            )
        return tuple(calls)

    async def _build_context(
        self,
        *,
        operation: SessionOperation,
        state: AgentRunState,
        package: AgentPackageVersion,
        effects: RuntimeEffects,
    ) -> ModelContext:
        step = state.current_step
        if step is None or step.phase != "preparing_request":
            raise RuntimeError("Context 只能为 preparing_request Step 构建")
        nodes = self._conversations.list_active_branch_nodes(
            session_id=operation.session_id
        )
        projected = ConversationProjector().project_conversation_messages(nodes)
        visible = tuple(
            apply_window(
                projected,
                turn_window=package.runtime_policy.context_turn_window,
            )
        )
        recalled = await effects.retrieve_recall_messages(
            session_id=operation.session_id,
            visible_messages=visible,
        )
        hook_contributions = await effects.invoke_hook(
            "before_request",
            BeforeRequestEvent(
                identity=ExecutionIdentity(
                    session_id=operation.session_id,
                    operation_id=operation.operation_id,
                    step_id=step.step_id,
                    step_sequence=step.step_sequence,
                ),
                visible_messages=visible,
                recall_messages=tuple(recalled),
            ),
        )
        if not isinstance(hook_contributions, ContextContributions):
            hook_contributions = ContextContributions()
        model_context = self._context_builder.build_model_context(
            package=package,
            visible_messages=visible,
            contributions=ContextContributions(
                system_sections=hook_contributions.system_sections,
                messages=tuple(recalled) + hook_contributions.messages,
            ),
        )
        # report 是 child→parent 的中间通信工具。它可以保留在冻结 Package
        # 中供 delegated Session 执行，但 Root 的 ModelContext 不应暴露它。
        # 这里仅收窄模型可见工具；执行端也由同一 allowlist 做校验。
        load_delegation = getattr(self._operations, "load_delegation", None)
        is_delegated = callable(load_delegation) and (
            load_delegation(operation.session_id) is not None
        )
        if not is_delegated:
            model_context = replace(
                model_context,
                tools=tuple(
                    tool for tool in model_context.tools if tool.name != "report"
                ),
            )
        if self._allowed_tool_names is not None:
            model_context = replace(
                model_context,
                tools=tuple(
                    tool
                    for tool in model_context.tools
                    if tool.name in self._allowed_tool_names
                ),
            )
        return model_context

    def _resolve_delegation_intent(
        self,
        *,
        operation: SessionOperation,
        package: AgentPackageVersion,
        call: ToolCallState,
    ) -> DelegateAgentIntent:
        delegation_policy = package.delegation_policy
        target_agent_id = str(
            call.arguments.get("agent_id") or delegation_policy.default_agent_id
        )
        if target_agent_id not in delegation_policy.allowed_agent_ids:
            raise ValueError(f"Agent '{target_agent_id}' 不在 Parent allowlist")

        if package.format_version in {1, 2}:
            if target_agent_id != package.agent_id:
                raise ValueError("历史格式 Delegation 只能使用 same-package")
            return DelegateAgentIntent(package.package_version_id)
        if package.format_version != 3:
            raise ValueError("不支持的 Agent Package format")
        if self._delegation_package_resolver is None:
            raise ValueError("format 3 Delegation 必须提供 child Package 解析器")
        child_package = self._delegation_package_resolver(
            operation, package, target_agent_id
        )
        if child_package.format_version != 3:
            raise ValueError("Delegation child Package 必须是 format 3")
        if child_package.agent_id != target_agent_id:
            raise ValueError("Delegation child Package 与目标 Agent 不一致")
        parent_tools = (
            self._allowed_tool_names
            if self._allowed_tool_names is not None
            else frozenset(tool.name for tool in package.tools)
        )
        if not parent_tools.intersection(tool.name for tool in child_package.tools):
            raise ValueError("Delegation Parent 与 child 的 Tool 权限交集为空")
        return DelegateAgentIntent(child_package.package_version_id)

    def _pending_step_messages(self, session_id: str) -> tuple[InboxMessage, ...]:
        return self._operations.list_pending_step_messages(session_id=session_id)

    def _result(
        self,
        operation: SessionOperation,
        state: AgentRunState,
        assistant_message: AssistantMessage | None = None,
        *,
        usage_leaf: str | None | object = _AUTO_USAGE_LEAF,
    ) -> OperationDriveResult:
        """统一构造结果，并从明确终点投影本次 Operation 的用量。"""
        if usage_leaf is _AUTO_USAGE_LEAF:
            if state.status == "succeeded":
                usage_leaf = state.final_assistant_node_id
            elif state.status in {"waiting", "cancelling"}:
                usage_leaf = self._current_reliable_leaf(operation)
            else:
                usage_leaf = None

        usage = None
        if usage_leaf is not None:
            nodes = self._conversations.list_branch_nodes(
                session_id=operation.session_id,
                leaf_node_id=usage_leaf,
            )
            usage = project_agent_run_usage(nodes, operation.input_node_id)
        return OperationDriveResult(
            operation_id=operation.operation_id,
            status=state.status,
            state=state,
            assistant_message=assistant_message,
            usage=usage,
        )

    def _current_reliable_leaf(self, operation: SessionOperation) -> str | None:
        """读取当前 Session 活动位置作为 waiting/本次失败的可靠 leaf。"""
        return self._conversations.load_conversation_session(
            operation.session_id
        ).active_node_id

    def _wake_sessions(self, session_ids: tuple[str, ...]) -> None:
        if self._wake_callback is None:
            return
        for session_id in session_ids:
            self._wake_callback(session_id)

    def _wake_parent(self, operation_id: str) -> None:
        if self._wake_callback is None and self._terminal_callback is None:
            return
        parent_session_id = self._operations.parent_session_id(operation_id)
        if parent_session_id is None:
            return
        if self._terminal_callback is not None:
            try:
                self._terminal_callback(parent_session_id)
            except Exception:
                logger.exception(
                    "调度 settled Parent 激活失败: session_id=%s", parent_session_id
                )
            return
        if self._wake_callback is not None:
            try:
                self._wake_callback(parent_session_id)
            except Exception:
                # 终态已经持久化；唤醒失败由恢复扫描兜底，不能反转结果。
                logger.exception(
                    "唤醒 settled Parent 失败: session_id=%s", parent_session_id
                )

    def _claim_step_messages(
        self,
        *,
        operation: SessionOperation,
        previous: AgentRunState,
        candidate: AgentRunState,
    ) -> bool:
        """以当前 Step 快照一次性 claim steer/inject。"""
        pending = self._pending_step_messages(operation.session_id)
        if not pending:
            return False
        return self._operations.claim_step_messages(
            message_ids=tuple(item.message_id for item in pending),
            state=candidate,
            expected_revision=previous.revision,
            updated_at=self._now(),
        )

    def _commit(
        self,
        state: AgentRunState,
        previous: AgentRunState,
        *,
        message: AgentMessage | None = None,
        node_id: str | None = None,
    ) -> AgentRunState:
        next_state = replace(state, revision=previous.revision + 1)
        node = None
        if message is not None:
            if node_id is None:
                raise ValueError("提交 AgentMessage 时必须提供 node_id")
            operation = self._operations.load_operation(next_state.operation_id)
            session = self._conversations.load_conversation_session(
                operation.session_id
            )
            node = ConversationNode(
                node_id=node_id,
                session_id=operation.session_id,
                parent_node_id=session.active_node_id,
                content_type="agent_message",
                content=message,
                created_at=self._now(),
            )
        if not self._operations.commit_transition(
            state=next_state,
            expected_revision=previous.revision,
            node=node,
        ):
            raise RuntimeError("AgentRunState CAS 冲突，停止推进且不重放副作用")
        return next_state


def _fingerprint(context: ModelContext) -> str:
    return hashlib.sha256(context.to_json().encode("utf-8")).hexdigest()


def _rejected_tool_call(
    block: ToolCallBlock,
    *,
    arguments: dict[str, object],
    reason: str,
    replay_policy: ToolReplayPolicy = "never",
) -> ToolCallState:
    return ToolCallState(
        tool_call_id=block.id,
        tool_name=block.name,
        arguments=arguments,
        status="rejected",
        approval=None,
        replay_policy=replay_policy,
        execution_intent=None,
        decision_reason=reason,
        result_node_id=None,
        is_error=None,
    )


def _tool_result_text(message: ToolResultMessage) -> str:
    return "\n".join(
        block.text for block in message.content if isinstance(block, TextBlock)
    )
