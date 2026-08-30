"""OperationDriver：推进一个已接受的 SessionOperation。

接受 Inbox、恢复 active Operation 属于 AgentDriver；本类只消费一个已有
Operation，并严格执行 ``intent commit -> 外部副作用 -> 结果 commit``。
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from uuid import uuid4

from pickel.agents.agent_package import AgentPackageVersion, AgentRuntimePolicy
from pickel.agents.agent_package_loader import PackageLoadError
from pickel.context.model_context import ModelContext
from pickel.context.model_context import SystemContent, SystemSection
from pickel.context.context_usage import model_context_fingerprint
from pickel.context.history_compaction import (
    HistoryCompactionError,
    HistoryCompactionGenerator,
    SummarizerSender,
)
from pickel.context.model_context_builder import (
    ContextContributions,
    ModelContextBuilder,
)
from pickel.shared.collaboration import (
    CollaborationState,
    PLAN_READ_ONLY_TOOL_NAMES,
)
from pickel.context.projection import ConversationProjector
from pickel.context.token_preflight import (
    ContextCompactionRequired,
    TokenPreflightResult,
    preflight_model_context,
)
from pickel.conversations.agent_message import (
    AgentMessage,
    AssistantMessage,
    ToolResultMessage,
    UserMessage,
    agent_message_to_dict,
)
from pickel.conversations.content_blocks import ArtifactBlock, TextBlock, ToolCallBlock
from pickel.conversations.conversation_service import ConversationService
from pickel.conversations.conversation_node import ConversationNode, HistoryCompaction
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
from pickel.telemetry.records import ErrorInfo, SpanTimer
from pickel.runtime.model_call_send_gate import (
    ModelCallSendConflict,
    ModelCallSendFailure,
    ModelCallSendGate,
)
from pickel.providers.stream import StreamDelta
from pickel.providers.errors import ProviderRequestError, classify_provider_error
from pickel.runtime.agent_run_usage import AgentRunUsage, project_agent_run_usage
from pickel.runtime.runtime_effects import RuntimeEffects
from pickel.runtime.worker_call_sender import (
    WorkerCallSendError,
    WorkerSendEffect,
)
from pickel.runtime.runtime_events import (
    RuntimeEventBase,
    ToolCallCompleted,
    ToolCallStarted,
)
from pickel.shared.event_envelope import EventEnvelope
from pickel.shared.frozen_json import freeze_json_object, thaw_json
from pickel.shared.execution_identity import ExecutionIdentity
from pickel.runtime.goal_verifier import (
    GoalVerification,
    build_goal_verification_prompt,
    parse_goal_verification,
)
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


def _compaction_threshold(package: AgentPackageVersion) -> int | None:
    """只从冻结 primary ModelVersion 读取本次请求的压缩阈值。"""
    model_policy = getattr(package, "model_policy", None)
    model = getattr(model_policy, "primary", None)
    if model is None:
        return None
    resolver = getattr(model, "effective_input_token_limit", None)
    if not callable(resolver):
        return None
    try:
        return resolver()
    except (TypeError, ValueError):
        return None


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
        model_call_send_gate: ModelCallSendGate | None = None,
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
        history_compaction_generator: HistoryCompactionGenerator | None = None,
        worker_sender: WorkerSendEffect | None = None,
        collaboration_state_provider: (
            Callable[[str], CollaborationState | None] | None
        ) = None,
    ) -> None:
        self._operations = operation_service
        self._conversations = conversation_service
        self._package_loader = package_loader
        self._effects_resolver = effects_resolver
        self._now = now or (lambda: datetime.now(timezone.utc))
        candidate = getattr(operation_service, "_store", None)
        if model_call_service is None:
            if candidate is not None and hasattr(candidate, "model_call_content_store"):
                model_call_service = ModelCallService(candidate)
        self._model_calls = model_call_service
        send_gate = model_call_send_gate
        if (
            send_gate is None
            and candidate is not None
            and hasattr(candidate, "transition_model_call")
        ):
            send_gate = ModelCallSendGate(candidate, now=self._now)
        elif send_gate is None and model_call_service is not None:
            # 仅兼容测试替身；正式 ModelCallService 不暴露 Store。
            service_store = getattr(model_call_service, "store", None)
            if service_store is not None:
                send_gate = ModelCallSendGate(service_store, now=self._now)
        self._history_compaction_generator = history_compaction_generator
        self._worker_sender = worker_sender
        self._collaboration_state_provider = collaboration_state_provider
        self._goal_feedback: dict[str, str] = {}
        self._model_call_send_gate = send_gate
        self._release_operation_package = release_operation_package
        self._context_builder = model_context_builder or ModelContextBuilder()
        self._step_id = step_id_factory or (lambda: str(uuid4()))
        self._node_id = node_id_factory or (lambda: str(uuid4()))
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
            self._goal_feedback.pop(operation_id, None)
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
        compaction_step_id: str | None = None

        while True:
            if state.status in {"succeeded", "failed", "cancelled"}:
                return self._result(operation, state, last_assistant)
            if state.status == "waiting":
                return self._result(operation, state, last_assistant)

            if state.status == "cancelling":
                cancellation = self._drive_cancellation(
                    operation=operation,
                    state=state,
                    last_assistant=last_assistant,
                )
                if isinstance(cancellation, OperationDriveResult):
                    return cancellation
                state = cancellation
                continue

            if state.status == "queued":
                state = self._commit(replace(state, status="running"), state)
                continue

            step = state.current_step
            if step is None:
                started = self._start_model_step(
                    operation=operation,
                    state=state,
                    package=package,
                    last_assistant=last_assistant,
                )
                if isinstance(started, OperationDriveResult):
                    return started
                state = started
                continue

            if step.phase == "preparing_request":
                prepared, compaction_step_id = await self._prepare_model_request(
                    operation=operation,
                    state=state,
                    package=package,
                    effects=effects,
                    last_assistant=last_assistant,
                    compaction_step_id=compaction_step_id,
                )
                if isinstance(prepared, OperationDriveResult):
                    return prepared
                state = prepared
                continue

            if step.phase == "request_ready":
                requested, assistant = await self._send_model_request(
                    operation=operation,
                    state=state,
                    package=package,
                    effects=effects,
                    consume_delta=consume_delta,
                )
                if isinstance(requested, OperationDriveResult):
                    return requested
                state = requested
                if assistant is not None:
                    last_assistant = assistant
                continue

            advanced = await self._drive_awaiting_tools(
                operation=operation,
                state=state,
                package=package,
                effects=effects,
                last_assistant=last_assistant,
                consume_tool_event=consume_tool_event,
                host_calls=host_calls,
            )
            if isinstance(advanced, OperationDriveResult):
                return advanced
            state = advanced
            continue

    async def _send_model_request(
        self,
        *,
        operation: SessionOperation,
        state: AgentRunState,
        package: AgentPackageVersion,
        effects: RuntimeEffects,
        consume_delta: StreamDeltaConsumer | None,
    ) -> tuple[AgentRunState | OperationDriveResult, AssistantMessage | None]:
        if self._model_calls is None:
            raise RuntimeError("OperationDriver 未配置 ModelCallService")
        step = state.current_step
        assert step is not None and step.request_intent is not None
        max_attempts = getattr(package.runtime_policy, "model_request_max_attempts", 3)
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
                    message=call_error.message if call_error is not None else str(exc),
                    retryable=(
                        bool(call_error.retryable) if call_error is not None else True
                    ),
                ),
            )
            state = self._commit(failed, state)
            return (
                self._result(
                    operation,
                    state,
                    usage_leaf=self._current_reliable_leaf(operation),
                ),
                None,
            )
        except (ModelCallPrepareConflict, ModelCallRecoveryError) as exc:
            raise RuntimeError(str(exc)) from exc

        state = prepared_call.state
        retry_after = prepared_call.retry_after_attempt
        if retry_after is not None:
            # attempt 间递增退避；旧 Package 的退避表已在解码时按历史公式合成。
            delay_ms = package.runtime_policy.retry_delay_ms(retry_after - 1)
            await self._sleep(delay_ms / 1000)

        gate = self._model_call_send_gate
        if gate is None:
            raise RuntimeError("未配置 ModelCallSendGate")
        try:
            response = await gate.send(
                call=prepared_call.model_call,
                prepared=prepared_call.prepared,
                effects=effects,
                consume_delta=consume_delta,
            )
        except ModelCallSendFailure as exc:
            error = classify_provider_error(exc.cause)
            failed_call = self._model_calls.record_send_failure(
                exc.call,
                exc.cause,
                first_chunk_at=exc.first_chunk_at,
            )
            if error.code == "context_window_exceeded":
                # 溢出重试原请求注定失败：先尝试压缩恢复，失败则按原错误终态。
                recovered = await self._recover_context_overflow(
                    operation=operation,
                    state=state,
                    package=package,
                    effects=effects,
                )
                if recovered is not None:
                    return recovered, None
            if package.runtime_policy.should_retry_request(
                retryable=error.retryable,
                first_chunk_received=exc.first_chunk_at is not None,
                completed_attempts=failed_call.request_attempt,
            ):
                return state, None
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
            return (
                self._result(
                    operation,
                    state,
                    usage_leaf=self._current_reliable_leaf(operation),
                ),
                None,
            )
        except ModelCallSendConflict as exc:
            raise RuntimeError(str(exc)) from exc

        response_content_ref = None
        save_response_content = getattr(
            self._model_calls, "save_response_content", None
        )
        if save_response_content is not None:
            response_content_ref = save_response_content(
                response, identity=prepared_call.model_call.identity
            )
        return await self._commit_model_response(
            operation=operation,
            state=state,
            package=package,
            effects=effects,
            prepared_call=prepared_call,
            response=response,
            response_content_ref=response_content_ref,
        )

    async def _commit_model_response(
        self,
        *,
        operation: SessionOperation,
        state: AgentRunState,
        package: AgentPackageVersion,
        effects: RuntimeEffects,
        prepared_call,
        response,
        response_content_ref,
    ) -> tuple[AgentRunState | OperationDriveResult, AssistantMessage | None]:
        assert self._model_calls is not None
        step = state.current_step
        assert step is not None and step.request_intent is not None
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
            return (
                self._result(
                    operation,
                    failed_state,
                    response.assistant_message,
                    usage_leaf=self._current_reliable_leaf(operation),
                ),
                None,
            )

        has_waiting_approval = any(
            call.status == "waiting_approval" for call in tool_calls
        )
        next_state = replace(
            state,
            revision=state.revision + 1,
            status="waiting" if has_waiting_approval else "running",
            waiting_reason=("tool_approval" if has_waiting_approval else None),
            current_step=replace(
                step,
                phase="awaiting_tools",
                request_intent=None,
                assistant_message_node_id=assistant_node_id,
                tool_calls=tool_calls,
            ),
        )
        session = self._conversations.load_conversation_session(operation.session_id)
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
        return next_state, response.assistant_message

    async def _prepare_model_request(
        self,
        *,
        operation: SessionOperation,
        state: AgentRunState,
        package: AgentPackageVersion,
        effects: RuntimeEffects,
        last_assistant: AssistantMessage | None,
        compaction_step_id: str | None,
    ) -> tuple[AgentRunState | OperationDriveResult, str | None]:
        step = state.current_step
        assert step is not None and step.phase == "preparing_request"
        pending = self._pending_step_messages(operation.session_id)
        if pending:
            candidate = replace(state, revision=state.revision + 1)
            if self._claim_step_messages(
                operation=operation,
                previous=state,
                candidate=candidate,
            ):
                return candidate, compaction_step_id
            refreshed = self._operations.load_agent_run_state(operation.operation_id)
            if refreshed.revision != state.revision:
                raise RuntimeError("AgentRunState CAS 冲突，停止推进且不重放副作用")
            if self._pending_step_messages(operation.session_id):
                raise RuntimeError("Step 消息 claim 冲突，停止推进")

        context = await self._build_context(
            operation=operation,
            state=state,
            package=package,
            effects=effects,
        )
        try:
            await preflight_model_context(
                context=context,
                provider=effects.provider,
                compaction_threshold=_compaction_threshold(package),
            )
        except ContextCompactionRequired as required:
            if compaction_step_id == step.step_id:
                return (
                    self._fail_preflight(
                        operation=operation,
                        state=state,
                        last_assistant=last_assistant,
                        code="history_compaction_no_progress",
                        message="历史压缩后 Context 仍达到安全阈值，停止重复压缩",
                    ),
                    compaction_step_id,
                )
            try:
                await self._execute_history_compaction(
                    operation=operation,
                    context=context,
                    package=package,
                    effects=effects,
                    preflight=required.result,
                )
                # 压缩已提交：重新预检重建后的投影。
                return state, step.step_id
            except (HistoryCompactionError, WorkerCallSendError) as exc:
                code = (
                    exc.code
                    if isinstance(exc, HistoryCompactionError)
                    else "worker_send_failed"
                )
                if code in {
                    "history_compaction_unavailable",
                    "history_compaction_no_progress",
                }:
                    # 配置缺失与结构性超限必须显式失败，不做静默降级。
                    return (
                        self._fail_preflight(
                            operation=operation,
                            state=state,
                            last_assistant=last_assistant,
                            code=code,
                            message=str(exc),
                        ),
                        step.step_id,
                    )
                # 压缩失败优雅降级：sender 重试额度已耗尽或摘要无法产出，
                # 记录诊断后直接以全量 Context 提交 Intent；provider 若拒绝
                # 会在发送路径显式失败。标记本 step 已尝试压缩，防止同一
                # step 内的重入形成压缩循环。
                logger.warning(
                    "历史压缩失败，降级为全量 Context 继续 operation=%s code=%s: %s",
                    operation.operation_id,
                    code,
                    exc,
                )
                compaction_step_id = step.step_id
            except Exception as exc:
                return (
                    self._fail_preflight(
                        operation=operation,
                        state=state,
                        last_assistant=last_assistant,
                        code="history_compaction_failed",
                        message=f"HistoryCompaction 生成或提交失败: {exc}",
                    ),
                    step.step_id,
                )

        intent = ModelRequestIntent(
            model_context=context,
            context_fingerprint=model_context_fingerprint(context),
        )
        next_state = replace(
            state,
            revision=state.revision + 1,
            current_step=replace(
                step,
                phase="request_ready",
                request_intent=intent,
            ),
        )
        if self._operations.commit_transition(
            state=next_state,
            expected_revision=state.revision,
            node=None,
        ):
            return next_state, None
        refreshed = self._operations.load_agent_run_state(operation.operation_id)
        if refreshed.revision == state.revision and self._pending_step_messages(
            operation.session_id
        ):
            return refreshed, None
        raise RuntimeError("AgentRunState CAS 冲突，停止推进且不重放副作用")

    def _fail_preflight(
        self,
        *,
        operation: SessionOperation,
        state: AgentRunState,
        last_assistant: AssistantMessage | None,
        code: str,
        message: str,
    ) -> OperationDriveResult:
        failed = replace(
            state,
            revision=state.revision + 1,
            status="failed",
            current_step=None,
            error=AgentRunError(
                code=code,
                message=message,
                retryable=False,
            ),
        )
        state = self._commit(failed, state)
        return self._result(
            operation,
            state,
            last_assistant,
            usage_leaf=self._current_reliable_leaf(operation),
        )

    def _drive_cancellation(
        self,
        *,
        operation: SessionOperation,
        state: AgentRunState,
        last_assistant: AssistantMessage | None,
    ) -> AgentRunState | OperationDriveResult:
        wake_sessions = self._operations.reconcile_cancellation(
            operation.operation_id,
            reason=(
                state.cancellation.cause if state.cancellation is not None else None
            ),
        )
        self._wake_sessions(wake_sessions)
        refreshed = self._operations.load_agent_run_state(operation.operation_id)
        if refreshed.revision != state.revision:
            return refreshed
        step = state.current_step
        if step is not None and any(
            call.status == "intent_recorded" and call.replay_policy == "never"
            for call in step.tool_calls
        ):
            return self._result(operation, state, last_assistant)
        if not self._operations.cancellation_ready(operation.operation_id):
            return self._result(operation, state, last_assistant)
        usage_leaf = self._current_reliable_leaf(operation)
        cancelled = replace(state, status="cancelled", current_step=None)
        if not self._operations.commit_transition(
            state=replace(cancelled, revision=state.revision + 1),
            expected_revision=state.revision,
            node=None,
            updated_at=self._now(),
        ):
            refreshed = self._operations.load_agent_run_state(operation.operation_id)
            if refreshed.revision != state.revision:
                return refreshed
            return self._result(operation, state, last_assistant)
        state = replace(cancelled, revision=state.revision + 1)
        return self._result(
            operation,
            state,
            last_assistant,
            usage_leaf=usage_leaf,
        )

    def _start_model_step(
        self,
        *,
        operation: SessionOperation,
        state: AgentRunState,
        package: AgentPackageVersion,
        last_assistant: AssistantMessage | None,
    ) -> AgentRunState | OperationDriveResult:
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
        if not self._pending_step_messages(operation.session_id):
            return self._commit(candidate, state)
        if self._claim_step_messages(
            operation=operation,
            previous=state,
            candidate=candidate,
        ):
            return candidate
        return self._commit(candidate, state)

    async def _execute_history_compaction(
        self,
        *,
        operation: SessionOperation,
        context: ModelContext,
        package: AgentPackageVersion,
        effects: RuntimeEffects,
        preflight: TokenPreflightResult | None = None,
    ) -> None:
        """执行一次压缩并追加节点；失败语义由 HistoryCompactionError.code 表达。

        最近节点已是压缩节点时拒绝执行：上一次压缩后 Context 仍超限属于
        结构性超限，重复压缩无法修复。
        """
        branch_nodes = self._conversations.list_active_branch_nodes(
            session_id=operation.session_id
        )
        if branch_nodes and branch_nodes[-1].content_type == "history_compaction":
            raise HistoryCompactionError(
                "history_compaction_no_progress",
                "最近节点已是历史压缩，Context 仍超限，停止重复压缩",
            )
        generator = self._history_compaction_generator
        if generator is None:
            raise HistoryCompactionError(
                "history_compaction_unavailable",
                "当前 Runtime 未配置 HistoryCompactionGenerator",
            )
        if effects.worker_provider is None:
            raise HistoryCompactionError(
                "history_compaction_unavailable",
                "当前 Operation 未配置 worker model",
            )
        content = await generator.generate(
            nodes=branch_nodes,
            model_context=context,
            preflight=preflight,
            send_summarizer=self._summarizer_sender_for(
                session_id=operation.session_id,
                effects=effects,
                runtime_policy=package.runtime_policy,
            ),
            max_summary_tokens=package.runtime_policy.compaction_max_summary_tokens,
            preserve_tail_tokens=package.runtime_policy.compaction_tail_tokens,
        )
        if not isinstance(content, HistoryCompaction):
            raise HistoryCompactionError(
                "history_compaction_invalid_result",
                "HistoryCompactionGenerator 必须返回 HistoryCompaction",
            )
        self._conversations.append_history_compaction(
            session_id=operation.session_id,
            content=content,
        )

    async def _recover_context_overflow(
        self,
        *,
        operation: SessionOperation,
        state: AgentRunState,
        package: AgentPackageVersion,
        effects: RuntimeEffects,
    ) -> AgentRunState | None:
        """上下文溢出的恢复：强制压缩后回到准备阶段重建 Intent 并重试。

        溢出说明本地估算低估了真实占用，重发原请求注定失败。压缩守卫
        保证同一条溢出链路不会无限重复压缩；恢复不可用时返回 None，
        调用方按原溢出错误进入终态，根因不丢失。
        """
        step = state.current_step
        assert step is not None and step.request_intent is not None
        try:
            await self._execute_history_compaction(
                operation=operation,
                context=step.request_intent.model_context,
                package=package,
                effects=effects,
            )
        except (HistoryCompactionError, WorkerCallSendError) as exc:
            code = (
                exc.code
                if isinstance(exc, HistoryCompactionError)
                else "worker_send_failed"
            )
            logger.warning(
                "上下文溢出恢复失败 operation=%s code=%s: %s",
                operation.operation_id,
                code,
                exc,
            )
            return None
        except Exception as exc:
            logger.warning(
                "上下文溢出恢复异常 operation=%s: %s", operation.operation_id, exc
            )
            return None
        candidate = replace(
            state,
            revision=state.revision + 1,
            current_step=replace(
                step,
                phase="preparing_request",
                request_intent=None,
            ),
        )
        return self._commit(candidate, state)

    async def _drive_awaiting_tools(
        self,
        *,
        operation: SessionOperation,
        state: AgentRunState,
        package: AgentPackageVersion,
        effects: RuntimeEffects,
        last_assistant: AssistantMessage | None,
        consume_tool_event: RuntimeEventConsumer | None,
        host_calls,
    ) -> AgentRunState | OperationDriveResult:
        step = state.current_step
        assert step is not None
        if step.phase != "awaiting_tools":
            raise RuntimeError(f"未知 ModelStep phase: {step.phase}")
        if not step.tool_calls:
            return await self._finish_answer(
                operation=operation,
                state=state,
                last_assistant=last_assistant,
                effects=effects,
                runtime_policy=package.runtime_policy,
            )

        pending = next(
            (call for call in step.tool_calls if call.status != "completed"), None
        )
        if pending is not None and pending.status == "rejected":
            return self._record_rejected_tool(state=state, step=step, call=pending)

        executable: ToolCallState | None = None
        if pending is not None and pending.status == "ready":
            collaboration = (
                self._collaboration_state_provider(operation.session_id)
                if self._collaboration_state_provider is not None
                else None
            )
            if (
                collaboration is not None
                and collaboration.mode == "plan"
                and pending.tool_name not in PLAN_READ_ONLY_TOOL_NAMES
            ):
                return self._record_rejected_tool(
                    state=state,
                    step=step,
                    call=replace(
                        pending,
                        status="rejected",
                        decision_reason=(
                            "Plan 模式只允许只读工具：ls、glob、grep、read"
                        ),
                    ),
                )
            state, executable = self._record_tool_intent(
                operation=operation,
                state=state,
                step=step,
                package=package,
                call=pending,
            )
            if executable is None:
                return state
        elif pending is not None and pending.status == "intent_recorded":
            if pending.replay_policy == "never":
                state = self._commit(
                    replace(
                        state,
                        status="waiting",
                        waiting_reason="tool_reconciliation",
                    ),
                    state,
                )
                return self._result(operation, state, last_assistant)
            executable = pending

        if executable is not None:
            return await self._execute_tool(
                operation=operation,
                state=state,
                effects=effects,
                call=executable,
                consume_tool_event=consume_tool_event,
                host_calls=host_calls,
            )
        if all(call.status == "completed" for call in step.tool_calls):
            return self._commit(
                replace(
                    state,
                    current_step=None,
                    completed_step_count=state.completed_step_count + 1,
                    status="running",
                    waiting_reason=None,
                ),
                state,
            )
        raise RuntimeError("ToolCallState 存在无法推进的状态")

    async def _finish_answer(
        self,
        *,
        operation: SessionOperation,
        state: AgentRunState,
        last_assistant: AssistantMessage | None,
        effects: RuntimeEffects,
        runtime_policy: AgentRuntimePolicy,
    ) -> AgentRunState | OperationDriveResult:
        step = state.current_step
        assert step is not None and step.assistant_message_node_id is not None
        if last_assistant is None:
            for persisted in self._conversations.list_active_branch_nodes(
                session_id=operation.session_id
            ):
                if persisted.node_id == step.assistant_message_node_id and isinstance(
                    persisted.content, AssistantMessage
                ):
                    last_assistant = persisted.content
                    break
        collaboration = (
            self._collaboration_state_provider(operation.session_id)
            if self._collaboration_state_provider is not None
            else None
        )
        if collaboration is not None and collaboration.mode == "goal":
            try:
                verification = await self._verify_goal(
                    operation=operation,
                    state=state,
                    candidate=last_assistant,
                    effects=effects,
                    runtime_policy=runtime_policy,
                    goal=collaboration.goal,
                )
            except Exception as exc:  # noqa: BLE001 — Goal 验证失败必须 fail closed
                failed = replace(
                    state,
                    revision=state.revision + 1,
                    current_step=None,
                    status="failed",
                    final_assistant_node_id=None,
                    error=AgentRunError(
                        code="goal_verification_failed",
                        message=f"Goal 完成验证失败: {exc}",
                        retryable=True,
                    ),
                )
                return self._commit(failed, state)
            if not verification.passed:
                feedback = (
                    f"Goal verifier 判断尚未完成。原因：{verification.reason}\n"
                    f"下一步：{verification.next_action}"
                )
                self._goal_feedback[operation.operation_id] = feedback
                continue_state = replace(
                    state,
                    revision=state.revision + 1,
                    current_step=None,
                    status="running",
                    completed_step_count=state.completed_step_count + 1,
                    final_assistant_node_id=None,
                    error=None,
                )
                return self._commit(continue_state, state)
        next_state = replace(
            state,
            revision=state.revision + 1,
            current_step=None,
            completed_step_count=state.completed_step_count + 1,
            status="succeeded",
            final_assistant_node_id=step.assistant_message_node_id,
        )
        if self._operations.commit_transition(
            state=next_state,
            expected_revision=state.revision,
            node=None,
        ):
            return self._result(operation, next_state, last_assistant)
        refreshed = self._operations.load_agent_run_state(operation.operation_id)
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
        return continue_state

    async def _verify_goal(
        self,
        *,
        operation: SessionOperation,
        state: AgentRunState,
        candidate: AssistantMessage | None,
        effects: RuntimeEffects,
        runtime_policy: AgentRuntimePolicy,
        goal: str | None,
    ) -> GoalVerification:
        del state
        if not goal:
            raise ValueError("Goal 模式缺少目标")
        if effects.worker_provider is None:
            raise ValueError("Goal 模式需要配置 worker model")
        candidate_text = json.dumps(
            agent_message_to_dict(candidate) if candidate is not None else {},
            ensure_ascii=False,
            sort_keys=True,
        )
        context = ModelContext(
            system=SystemContent(
                sections=(
                    SystemSection(
                        name="goal_verification",
                        text=(
                            "你是独立的 Goal 完成验证器。只判断目标是否被候选结果证明，"
                            "不得调用工具，不得替候选结果补事实。"
                        ),
                    ),
                )
            ),
            messages=(
                UserMessage(
                    (TextBlock(build_goal_verification_prompt(goal, candidate_text)),)
                ),
            ),
            tools=(),
        )
        message = await self._summarizer_sender_for(
            session_id=operation.session_id,
            effects=effects,
            runtime_policy=runtime_policy,
        )(context=context, purpose="goal_verification")
        text = "\n".join(
            block.text
            for block in message.content
            if isinstance(block, TextBlock) and block.text
        )
        return parse_goal_verification(text)

    def _summarizer_sender_for(
        self,
        *,
        session_id: str,
        effects: RuntimeEffects,
        runtime_policy: AgentRuntimePolicy,
    ) -> SummarizerSender:
        """绑定本次 Operation 的 worker 身份与重试策略的窄发送回调。"""
        sender = self._worker_sender
        if sender is None:
            raise RuntimeError("OperationDriver 未配置 WorkerCallSender")

        async def send(*, context: ModelContext, purpose: str) -> AssistantMessage:
            return await sender(
                session_id=session_id,
                context=context,
                purpose=purpose,
                worker_provider=effects.worker_provider,
                runtime_policy=runtime_policy,
                provider_timeout_seconds=effects.provider_timeout_seconds,
            )

        return send

    def _record_rejected_tool(
        self,
        *,
        state: AgentRunState,
        step: ModelStepState,
        call: ToolCallState,
    ) -> AgentRunState:
        result_node_id = self._node_id()
        completed = replace(
            call,
            status="completed",
            result_node_id=result_node_id,
            is_error=True,
        )
        calls = tuple(
            completed if item.tool_call_id == call.tool_call_id else item
            for item in step.tool_calls
        )
        decision = call.approval.decision if call.approval else None
        reason = decision.reason if decision is not None else call.decision_reason
        content = "工具调用被拒绝"
        if reason:
            content = f"{content}：{reason}"
        return self._commit(
            replace(state, current_step=replace(step, tool_calls=calls)),
            state,
            message=ToolResultMessage(
                tool_call_id=call.tool_call_id,
                tool_name=call.tool_name,
                content=(TextBlock(text=content),),
                is_error=True,
            ),
            node_id=result_node_id,
        )

    def _record_tool_intent(
        self,
        *,
        operation: SessionOperation,
        state: AgentRunState,
        step: ModelStepState,
        package: AgentPackageVersion,
        call: ToolCallState,
    ) -> tuple[AgentRunState, ToolCallState | None]:
        intent_error: Exception | None = None
        execution_intent = call.execution_intent
        if call.tool_name == "delegate_agent":
            try:
                execution_intent = self._resolve_delegation_intent(
                    operation=operation,
                    package=package,
                    call=call,
                )
            except Exception as exc:
                intent_error = exc
        next_calls = tuple(
            (
                (
                    replace(
                        item,
                        status="rejected",
                        execution_intent=None,
                        decision_reason=f"delegate_agent 目标不可用: {intent_error}",
                    )
                    if intent_error is not None
                    else replace(
                        item,
                        status="intent_recorded",
                        execution_intent=execution_intent,
                    )
                )
                if item.tool_call_id == call.tool_call_id
                else item
            )
            for item in step.tool_calls
        )
        state = self._commit(
            replace(state, current_step=replace(step, tool_calls=next_calls)),
            state,
        )
        if intent_error is not None:
            return state, None
        assert state.current_step is not None
        return state, next(
            item
            for item in state.current_step.tool_calls
            if item.tool_call_id == call.tool_call_id
        )

    async def _execute_tool(
        self,
        *,
        operation: SessionOperation,
        state: AgentRunState,
        effects: RuntimeEffects,
        call: ToolCallState,
        consume_tool_event: RuntimeEventConsumer | None,
        host_calls,
    ) -> AgentRunState:
        assert state.current_step is not None
        identity = ExecutionIdentity(
            session_id=operation.session_id,
            operation_id=operation.operation_id,
            step_id=state.current_step.step_id,
            step_sequence=state.current_step.step_sequence,
            tool_call_id=call.tool_call_id,
        )
        await _consume_runtime_event(
            consume_tool_event,
            ToolCallStarted(
                envelope=EventEnvelope(identity=identity),
                tool_name=call.tool_name,
                arguments=thaw_json(call.arguments),
            ),
        )
        result = await effects.execute_tool_call(
            operation=operation,
            state=state,
            tool_call_id=call.tool_call_id,
            host_calls=host_calls,
        )
        if not isinstance(result, ToolResultMessage):
            raise RuntimeError("ToolEffect 必须返回已渲染 ToolResultMessage")
        if (
            result.tool_call_id != call.tool_call_id
            or result.tool_name != call.tool_name
        ):
            raise RuntimeError("ToolResultMessage 与 ToolCall 身份不一致")
        result_node_id = self._node_id()
        completed = replace(
            next(
                item
                for item in state.current_step.tool_calls
                if item.tool_call_id == call.tool_call_id
            ),
            status="completed",
            result_node_id=result_node_id,
            is_error=result.is_error,
        )
        calls = tuple(
            completed if item.tool_call_id == call.tool_call_id else item
            for item in state.current_step.tool_calls
        )
        state = self._commit(
            replace(state, current_step=replace(state.current_step, tool_calls=calls)),
            state,
            message=result,
            node_id=result_node_id,
        )
        await _consume_runtime_event(
            consume_tool_event,
            ToolCallCompleted(
                envelope=EventEnvelope(identity=identity),
                tool_name=call.tool_name,
                content=_tool_result_text(result),
                is_error=result.is_error,
            ),
        )
        return state

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
        identity = ExecutionIdentity(
            session_id=operation.session_id,
            operation_id=operation.operation_id,
            step_id=step.step_id,
            step_sequence=step.step_sequence,
        )
        context_span = SpanTimer("pickel.model_context.build", identity)
        try:
            context = await self._build_context_impl(
                operation=operation,
                state=state,
                package=package,
                effects=effects,
            )
        except asyncio.CancelledError:
            context_span.finish(status="cancelled")
            raise
        except Exception as exc:
            context_span.finish(
                status="error", error=ErrorInfo.from_exception(exc, kind="context")
            )
            raise
        else:
            context_span.finish()
            return context

    async def _build_context_impl(
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
        # 正式 ModelRequest 使用完整活动分支；Context 压缩由后续 token
        # preflight/HistoryCompaction 流程负责，不在这里静默截断历史。
        visible = tuple(projected)
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
        collaboration = (
            self._collaboration_state_provider(operation.session_id)
            if self._collaboration_state_provider is not None
            else None
        )
        system_sections = list(hook_contributions.system_sections)
        feedback = self._goal_feedback.get(operation.operation_id)
        if feedback:
            system_sections.append(
                SystemSection(name="goal_verification_feedback", text=feedback)
            )
        model_context = self._context_builder.build_model_context(
            package=package,
            visible_messages=visible,
            contributions=ContextContributions(
                system_sections=tuple(system_sections),
                messages=tuple(recalled) + hook_contributions.messages,
            ),
            collaboration=collaboration,
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
        if collaboration is not None and collaboration.mode == "plan":
            model_context = replace(
                model_context,
                tools=tuple(
                    tool
                    for tool in model_context.tools
                    if tool.name in PLAN_READ_ONLY_TOOL_NAMES
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
    parts: list[str] = []
    for block in message.content:
        if isinstance(block, TextBlock):
            parts.append(block.text)
        elif isinstance(block, ArtifactBlock):
            reference = block.artifact
            label = block.alt_text or reference.display_name or "artifact"
            parts.append(f"[artifact: {label} ({reference.media_type})]")
    return "\n".join(parts)
