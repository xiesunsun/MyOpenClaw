"""ConversationSession 的 Host/UI 适配层。"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any, Literal

from pickel.agents.agent_package import LoadedAgentPackage
from pickel.app.boot import CompositionStore
from pickel.app.runtime_models import (
    ContextInspection,
    ConversationClosedError,
    PendingSkillInfo,
    RuntimeErrorInfo,
    RuntimeSnapshot,
    SkillActionResult,
    ToolInfo,
    OperationInProgressError,
    AgentRunRequest,
    AgentRunResult,
)
from pickel.config.app_config import AppConfig
from pickel.context.model_context_builder import ModelContextBuilder
from pickel.context.projection import ConversationProjector
from pickel.context.window import apply_window
from pickel.context.context_usage import estimate_context_usage
from pickel.conversations.agent_message import AssistantMessage
from pickel.conversations.content_blocks import TextBlock, ToolCallBlock
from pickel.conversations.conversation_service import ConversationService
from pickel.conversations.conversation_session import ConversationSession
from pickel.operations.operation_service import OperationService
from pickel.runtime.conversation_output_bus import ConversationOutputBus
from pickel.runtime.event_bus import EventBus
from pickel.runtime.runtime_bus import RuntimeBus
from pickel.runtime.runtime_events import (
    AssistantMessageEvent,
    RuntimeEventHandler,
    TextDeltaEvent,
    ThinkingDeltaEvent,
    ToolCallArgsDeltaEvent,
    AgentRunCompleted,
    AgentRunFailed,
    AgentRunInterrupted,
    AgentRunStarted,
)
from pickel.observe.jsonl_trace_sink import (
    JsonlTraceSink,
    TraceOptions,
    trace_mode,
    trace_path,
)
from pickel.observe.operation_report import export_operation_report
from pickel.observe.records import (
    ErrorInfo,
    SpanTimer,
    observation_scope,
)
from pickel.providers.stream import (
    TextDelta,
    ThinkingDelta,
    ToolCallArgsDelta,
)
from pickel.app.runtime_generation import LoadedPackageHandle
from pickel.runtime.agent import Agent, AgentBusyError
from pickel.extensions_host.event_processor import EventProcessor
from pickel.shared.conversation_mode import ConversationMode
from pickel.shared.conversation_output import (
    ConversationOutputBase,
    ConversationOutputHandler,
)
from pickel.shared.event_envelope import EventEnvelope
from pickel.shared.execution_identity import ExecutionIdentity
from pickel.skills.store import SkillStoreError

_AppRunStatus = Literal["completed", "blocked", "failed", "cancelled"]


def _map_operation_status(operation_status: str | None) -> _AppRunStatus:
    """把 Operation 终态映射为 Application Surface 的稳定状态。"""

    if operation_status is None:
        return "blocked"
    if operation_status == "succeeded":
        return "completed"
    if operation_status in {"waiting", "cancelling"}:
        return "blocked"
    if operation_status == "failed":
        return "failed"
    if operation_status == "cancelled":
        return "cancelled"
    raise ValueError(f"未知 Operation status: {operation_status!r}")


class ConversationRuntime:
    """一个活动 ConversationSession 的控制与观察接口。"""

    def __init__(
        self,
        *,
        loaded_agent_package: LoadedAgentPackage,
        agent: Agent,
        session: ConversationSession,
        conversation_service: ConversationService,
        operation_service: OperationService,
        persistence_store: CompositionStore,
        persistence: str,
        app_config: AppConfig,
        mode: ConversationMode = "batch",
        loaded_package_handle: LoadedPackageHandle | None = None,
        skill_store: Any | None = None,
        on_detach: Callable[[], None] | None = None,
    ) -> None:
        self._loaded_agent_package = loaded_agent_package
        self._agent = agent
        self._session = session
        self._conversation_service = conversation_service
        self._operation_service = operation_service
        self._persistence_store = persistence_store
        self._skill_store = skill_store
        self._on_detach = on_detach
        self._persistence = persistence
        self._app_config = app_config
        self._mode = mode
        # 该 Handle 同时保持 LoadedAgentPackage 与所属 Generation 存活。
        self._loaded_package_handle = loaded_package_handle
        self._runtime_generation = (
            loaded_package_handle.generation
            if loaded_package_handle is not None
            else None
        )
        self._runtime_bus = RuntimeBus()
        self._events = self._runtime_bus.events
        self._outputs = ConversationOutputBus()
        self._closed = False
        self._background_tasks: set[asyncio.Task[None]] = set()
        self._event_processors: list[tuple[EventProcessor, Callable[[], None]]] = []
        self._trace_sink: JsonlTraceSink | None = None
        self._unsubscribe_trace: Callable[[], None] | None = None
        self._open_trace()

    @property
    def agent_definition(self):
        return self._loaded_agent_package.version

    @property
    def model_config(self):
        return self._loaded_agent_package.version.model_policy.primary

    @property
    def session(self) -> ConversationSession:
        return self._session

    @property
    def app_config(self) -> AppConfig:
        return self._app_config

    @property
    def mode(self) -> ConversationMode:
        return self._mode

    @property
    def persistence(self) -> str:
        return self._persistence

    @property
    def persistence_store(self) -> CompositionStore:
        """供 Host reload 保留 ephemeral Store；不暴露通用 RuntimeStore。"""
        return self._persistence_store

    @property
    def runtime_generation(self):
        """该 Conversation 创建时所属的 Generation（不新增引用计数）。"""

        return self._runtime_generation

    @property
    def event_bus(self) -> EventBus:
        return self._events

    @property
    def runtime_bus(self) -> RuntimeBus:
        return self._runtime_bus

    @property
    def closed(self) -> bool:
        return self._closed

    async def start_agent_run(self, request: AgentRunRequest) -> AgentRunResult:
        with observation_scope(self._trace_sink):
            return await self._execute_agent_run(request)

    async def _execute_agent_run(self, request: AgentRunRequest) -> AgentRunResult:
        if self._closed:
            raise ConversationClosedError("Conversation 已关闭")
        temporary_package_handle = None
        if self._runtime_generation is not None:
            temporary_package_handle = self._runtime_generation.acquire_loaded_package(
                self._loaded_agent_package.version.package_version_id
            )
        operation_id = self._session.active_operation_id or "pending"
        started = time.perf_counter()
        timer = SpanTimer(
            "pickel.agent_run",
            ExecutionIdentity(
                session_id=self._session.session_id,
                operation_id=operation_id,
            ),
        )

        async def consume_delta(delta, identity: ExecutionIdentity) -> None:
            envelope = EventEnvelope(identity=identity)
            if isinstance(delta, TextDelta):
                await self._events.emit(
                    TextDeltaEvent(envelope=envelope, text=delta.text)
                )
            elif isinstance(delta, ThinkingDelta):
                await self._events.emit(
                    ThinkingDeltaEvent(envelope=envelope, text=delta.text)
                )
            elif isinstance(delta, ToolCallArgsDelta):
                await self._events.emit(
                    ToolCallArgsDeltaEvent(
                        envelope=envelope,
                        partial_json=delta.partial_json,
                    )
                )

        try:
            result = await self._agent.followup_and_wait(
                request.message,
                consume_delta=consume_delta,
                host_calls=self._runtime_bus.host_calls.client,
            )
            if result.accepted is not None:
                operation_id = result.accepted.operation.operation_id
            elif self._session.active_operation_id is not None:
                operation_id = self._session.active_operation_id
            envelope = EventEnvelope(
                identity=ExecutionIdentity(
                    session_id=self._session.session_id,
                    operation_id=operation_id,
                )
            )
            await self._events.emit(
                AgentRunStarted(envelope=envelope, user_text=self._user_text(request))
            )
            elapsed_ms = round((time.perf_counter() - started) * 1000)
            operation_result = result.operation_result
            # usage 是 Driver 针对本次 Operation 捕获的模型请求合计；只读取一次，
            # 并将同一个不可变值广播给所有 Application Surface 消费者。
            usage = operation_result.usage if operation_result is not None else None
            message = (
                operation_result.assistant_message
                if operation_result is not None
                else None
            )
            operation_status = (
                operation_result.status if operation_result is not None else "waiting"
            )
            app_status = _map_operation_status(
                operation_status if operation_result is not None else None
            )

            if message is not None:
                await self._events.emit(
                    AssistantMessageEvent(
                        envelope=envelope,
                        text=self._assistant_text(message),
                        usage=usage,
                    )
                )

            if app_status == "failed":
                assert operation_result is not None
                state_error = operation_result.state.error
                if state_error is None:
                    raise RuntimeError("failed Operation 缺少持久化 error")
                error = RuntimeErrorInfo(
                    error_type=state_error.code,
                    message=state_error.message,
                )
                await self._events.emit(
                    AgentRunFailed(
                        envelope=envelope,
                        error_type=error.error_type,
                        message=error.message,
                    )
                )
                timer.finish(
                    status="error",
                    attributes={"outcome": app_status},
                    error=ErrorInfo(
                        kind="agent_run",
                        type=error.error_type,
                        message=error.message,
                        retryable=getattr(state_error, "retryable", None),
                    ),
                )
                self._refresh_session()
                return AgentRunResult(
                    status=app_status,
                    session_id=self._session.session_id,
                    operation_id=operation_id,
                    message=message,
                    usage=usage,
                    elapsed_ms=elapsed_ms,
                    error=error,
                )

            await self._events.emit(
                AgentRunCompleted(
                    envelope=envelope,
                    usage=usage,
                    elapsed_ms=elapsed_ms,
                    outcome=app_status,
                )
            )
            timer.finish(
                status="cancelled" if app_status == "cancelled" else "ok",
                attributes={"outcome": app_status},
            )
            self._refresh_session()
            return AgentRunResult(
                status=app_status,
                session_id=self._session.session_id,
                operation_id=operation_id,
                message=message,
                usage=usage,
                elapsed_ms=elapsed_ms,
            )
        except asyncio.CancelledError:
            self._agent.cancel(reason="用户中断")
            envelope = EventEnvelope(
                identity=ExecutionIdentity(
                    session_id=self._session.session_id,
                    operation_id=operation_id,
                )
            )
            await self._events.emit(
                AgentRunInterrupted(envelope=envelope, at_step=0, partial_text="")
            )
            timer.finish(
                status="cancelled",
                attributes={"outcome": "cancelled"},
            )
            self._refresh_session()
            raise
        except AgentBusyError as exc:
            raise OperationInProgressError(
                "当前 Conversation 已有 Operation 正在执行"
            ) from exc
        except Exception as exc:
            elapsed_ms = round((time.perf_counter() - started) * 1000)
            envelope = EventEnvelope(
                identity=ExecutionIdentity(
                    session_id=self._session.session_id,
                    operation_id=operation_id,
                )
            )
            await self._events.emit(
                AgentRunFailed(
                    envelope=envelope,
                    error_type=type(exc).__name__,
                    message=str(exc),
                )
            )
            timer.finish(
                status="error",
                attributes={"outcome": "failed"},
                error=ErrorInfo.from_exception(exc, kind="agent_run"),
            )
            self._refresh_session()
            return AgentRunResult(
                status="failed",
                session_id=self._session.session_id,
                operation_id=operation_id,
                message=None,
                usage=None,
                elapsed_ms=elapsed_ms,
                error=RuntimeErrorInfo(
                    error_type=type(exc).__name__,
                    message=str(exc),
                ),
            )
        finally:
            if temporary_package_handle is not None:
                temporary_package_handle.close_sync()

    def subscribe(self, handler: RuntimeEventHandler) -> Callable[[], None]:
        return self._events.subscribe(handler)

    def subscribe_outputs(
        self,
        handler: ConversationOutputHandler,
    ) -> Callable[[], None]:
        return self._outputs.subscribe(handler)

    async def publish_output(self, output: ConversationOutputBase) -> None:
        await self._outputs.publish(output)

    def snapshot(self) -> RuntimeSnapshot:
        preview = self._conversation_service.build_conversation_preview(self._session)
        version = self._loaded_agent_package.version
        thinking = version.model_policy.primary.provider_options.get("thinking")
        return RuntimeSnapshot(
            session_id=preview.session_id,
            agent_id=preview.agent_id,
            status=preview.status,
            message_count=preview.message_count,
            updated_at=preview.updated_at,
            last_message=preview.last_message,
            model_id=(
                f"{version.model_policy.primary.provider}/"
                f"{version.model_policy.primary.model}"
            ),
            thinking=str(thinking) if thinking is not None else None,
        )

    def list_tools(self) -> tuple[ToolInfo, ...]:
        return tuple(
            ToolInfo(
                name=entry.name,
                source=entry.source.value,
                origin=entry.origin,
                version=entry.version,
            )
            for entry in self._loaded_agent_package.tool_snapshot.entries
        )

    async def inspect_context(self) -> ContextInspection:
        # Session 可能在后台 AgentDriver 推进期间发生变化，因此以最新持久化
        # 指针查找当前 Operation；request_ready 时直接使用已提交 Intent，
        # 不重新执行 Context、Recall 或 Hook 管道。
        self._refresh_session()
        nodes = self._conversation_service.list_active_branch_nodes(
            session_id=self._session.session_id
        )
        source = "preview"
        context = None
        operation_id = self._session.active_operation_id
        if operation_id is not None:
            state = self._operation_service.load_agent_run_state(operation_id)
            step = state.current_step
            if step is not None and step.request_intent is not None:
                context = step.request_intent.model_context
                source = "model_request_intent"

        if context is None:
            messages = ConversationProjector().project_conversation_messages(nodes)
            visible = apply_window(
                messages,
                turn_window=self._loaded_agent_package.version.runtime_policy.context_turn_window,
            )
            context = ModelContextBuilder().build_model_context(
                package=self._loaded_agent_package.version,
                visible_messages=visible,
            )
        tool_calls = sum(
            1
            for message in context.messages
            for block in message.content
            if isinstance(block, ToolCallBlock)
        )
        version = self._loaded_agent_package.version
        return ContextInspection(
            usage=estimate_context_usage(
                context,
                model_label=(
                    f"{version.model_policy.primary.provider} / "
                    f"{version.model_policy.primary.model}"
                ),
                max_input_tokens=version.model_policy.primary.max_input_tokens,
            ),
            last_turn=None,
            session_total=None,
            note="本地字符估算；实际用量以 Provider 响应为准",
            turns=sum(1 for message in context.messages if message.role == "user"),
            tool_calls=tool_calls,
            compactions=sum(
                1 for node in nodes if node.content_type == "history_compaction"
            ),
            tool_definitions=len(context.tools),
            source=source,
        )

    def list_pending_skills(self) -> tuple[PendingSkillInfo, ...]:
        store = self._skill_store
        if store is None:
            return ()
        return tuple(
            PendingSkillInfo(
                pending_id=item.pending_id,
                action=item.action,
                skill_name=item.skill_name,
                agent_id=item.agent_id,
            )
            for item in store.list_pending()
        )

    def apply_skill_action(self, action: str, pending_id: str) -> SkillActionResult:
        store = self._skill_store
        if store is None:
            raise SkillStoreError("当前 Agent 未配置 skills 目录")
        if action == "diff":
            return SkillActionResult(action, pending_id, diff=store.diff(pending_id))
        if action == "approve":
            return SkillActionResult(
                action,
                pending_id,
                path=store.approve(pending_id),
            )
        if action == "reject":
            store.reject(pending_id)
            return SkillActionResult(action, pending_id)
        raise SkillStoreError(f"未知 skill 操作：{action}")

    def start_background_task(
        self,
        coroutine: Coroutine[Any, Any, None],
        name: str,
    ) -> None:
        task = asyncio.create_task(coroutine, name=name)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    def add_event_processor(
        self,
        processor: EventProcessor,
        event_types: tuple[type[Any], ...],
    ) -> None:
        async def consume(event) -> None:
            if isinstance(event, event_types):
                await processor.handle_event(event)

        self._event_processors.append((processor, self.subscribe(consume)))

    def flush(self) -> None:
        """新 Runtime 每次转换都已原子提交，无待刷缓冲。"""
        if self._trace_sink is not None:
            self._trace_sink.flush()

    def archive(self) -> None:
        if self._closed:
            return
        self._conversation_service.archive_conversation_session(
            session_id=self._session.session_id
        )
        self.detach()

    def detach(self) -> None:
        if self._closed:
            return
        self._runtime_bus.close_now()
        if self._unsubscribe_trace is not None:
            self._unsubscribe_trace()
            self._unsubscribe_trace = None
        if self._trace_sink is not None:
            self._trace_sink.close()
            self._trace_sink = None
        for processor, unsubscribe in reversed(self._event_processors):
            unsubscribe()
            processor.close()
        self._event_processors.clear()
        for task in tuple(self._background_tasks):
            task.cancel()
        self._background_tasks.clear()
        self._outputs.clear()
        self._close_loaded_package_handle()
        self._closed = True
        on_detach = self._on_detach
        self._on_detach = None
        if on_detach is not None:
            on_detach()

    async def close(self) -> None:
        self.detach()

    def _close_loaded_package_handle(self) -> None:
        handle = self._loaded_package_handle
        if handle is None:
            return
        self._loaded_package_handle = None
        handle.close_sync()

    def export_observation(self, out: Path | None = None) -> Path:
        self.flush()
        return export_operation_report(
            conversation_service=self._conversation_service,
            sessions=(self._session,),
            out=out,
        )

    def _open_trace(self) -> None:
        configured = self._app_config.observability.trace
        if configured is None:
            return
        mode = trace_mode(configured.mode)
        if mode == "off":
            return
        try:
            self._trace_sink = JsonlTraceSink(
                trace_path(self._session.session_id),
                TraceOptions(
                    mode=mode,
                    queue_capacity=configured.queue_capacity,
                    batch_size=configured.batch_size,
                    flush_interval_ms=configured.flush_interval_ms,
                    max_file_size_mb=configured.max_file_size_mb,
                    max_age_days=configured.max_age_days,
                    max_total_size_mb=configured.max_total_size_mb,
                ),
            )
        except OSError:
            return
        self._unsubscribe_trace = self.subscribe(self._trace_sink)

    def _refresh_session(self) -> None:
        self._session = self._conversation_service.load_conversation_session(
            self._session.session_id
        )

    @staticmethod
    def _user_text(request: AgentRunRequest) -> str:
        return "\n".join(
            block.text
            for block in request.message.content
            if isinstance(block, TextBlock)
        )

    @staticmethod
    def _assistant_text(message: AssistantMessage) -> str:
        return "\n".join(
            block.text for block in message.content if isinstance(block, TextBlock)
        )
