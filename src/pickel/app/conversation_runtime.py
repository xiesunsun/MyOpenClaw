"""基于 AgentRuntime 与 ConversationSession 的应用控制面。"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

from pickel.agents.agent_package import AgentDefinition, LoadedAgentPackage
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
from pickel.context.context_usage import estimate_context_usage
from pickel.conversations.agent_message import AssistantMessage
from pickel.conversations.content_blocks import TextBlock
from pickel.conversations.conversation_service import ConversationService
from pickel.conversations.conversation_session import ConversationSession
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
    ModelStepStarted,
    ToolCallCompleted,
    ToolCallSnapshot,
    ToolCallStarted,
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
    ObservationIdentity,
    SpanTimer,
    observation_scope,
)
from pickel.providers.stream import (
    TextDelta,
    ThinkingDelta,
    ToolCallArgsDelta,
)
from pickel.runtime.agent_runtime import AgentRuntime
from pickel.runtime.agent_run_progress import (
    ModelStepStartedProgress,
    ToolCallCompletedProgress,
    ToolCallStartedProgress,
)
from pickel.persistence.runtime_store import RuntimeStore
from pickel.extensions_host.event_processor import EventProcessor
from pickel.shared.conversation_mode import ConversationMode
from pickel.shared.conversation_output import (
    ConversationOutputBase,
    ConversationOutputHandler,
)
from pickel.shared.event_envelope import EventEnvelope
from pickel.skills.store import SkillStoreError


class ConversationRuntime:
    """一个活动 ConversationSession 的控制与观察接口。"""

    def __init__(
        self,
        *,
        loaded_agent_package: LoadedAgentPackage,
        agent_runtime: AgentRuntime,
        session: ConversationSession,
        conversation_service: ConversationService,
        runtime_store: RuntimeStore,
        persistence: str,
        app_config: AppConfig,
        mode: ConversationMode = "batch",
    ) -> None:
        self._loaded_agent_package = loaded_agent_package
        self._agent_runtime = agent_runtime
        self._session = session
        self._conversation_service = conversation_service
        self._runtime_store = runtime_store
        self._persistence = persistence
        self._app_config = app_config
        self._mode = mode
        self._runtime_bus = RuntimeBus()
        self._events = self._runtime_bus.events
        self._outputs = ConversationOutputBus()
        self._active_operation_id: str | None = None
        self._active_task: asyncio.Task[Any] | None = None
        self._closed = False
        self._control_lock = asyncio.Lock()
        self._background_tasks: set[asyncio.Task[None]] = set()
        self._event_processors: list[tuple[EventProcessor, Callable[[], None]]] = []
        self._trace_sink: JsonlTraceSink | None = None
        self._unsubscribe_trace: Callable[[], None] | None = None
        self._open_trace()

    @property
    def agent_definition(self) -> AgentDefinition:
        return self._loaded_agent_package.version.definition

    @property
    def model_config(self):
        return self._loaded_agent_package.model_config

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
    def runtime_store(self) -> RuntimeStore:
        """供 RuntimeHost 在 reload 时保留 ephemeral 数据。"""
        return self._runtime_store

    @property
    def event_bus(self) -> EventBus:
        return self._events

    @property
    def runtime_bus(self) -> RuntimeBus:
        return self._runtime_bus

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def active_operation_id(self) -> str | None:
        return self._active_operation_id

    async def start_agent_run(self, request: AgentRunRequest) -> AgentRunResult:
        with observation_scope(self._trace_sink):
            return await self._execute_agent_run(request)

    async def _execute_agent_run(self, request: AgentRunRequest) -> AgentRunResult:
        if self._closed:
            raise ConversationClosedError("Conversation 已关闭")
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("AgentRun 必须运行在 asyncio.Task 中")
        async with self._control_lock:
            if self._active_task is not None:
                raise OperationInProgressError(
                    "当前 Conversation 已有 Operation 正在执行"
                )
            accepted = await self._agent_runtime.accept_agent_run(
                session_id=self._session.session_id,
                user_message=request.message,
            )
            operation_id = accepted.operation.operation_id
            self._active_operation_id = operation_id
            self._active_task = task
        started = time.perf_counter()
        envelope = EventEnvelope(
            session_id=self._session.session_id,
            operation_id=operation_id,
        )
        timer = SpanTimer(
            "pickel.agent_run",
            ObservationIdentity(
                session_id=self._session.session_id,
                operation_id=operation_id,
            ),
        )
        await self._events.emit(
            AgentRunStarted(envelope=envelope, user_text=self._user_text(request))
        )

        async def consume_delta(delta) -> None:
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
                        tool_call_id=delta.tool_call_id,
                        partial_json=delta.partial_json,
                    )
                )

        async def consume_progress(progress) -> None:
            progress_envelope = EventEnvelope(
                session_id=self._session.session_id,
                operation_id=progress.operation_id,
                step_id=progress.step_id,
                step_sequence=progress.step_sequence,
            )
            if isinstance(progress, ModelStepStartedProgress):
                await self._events.emit(ModelStepStarted(envelope=progress_envelope))
                return
            tool_call = ToolCallSnapshot(
                tool_call_id=progress.tool_call.tool_call_id,
                tool_name=progress.tool_call.tool_name,
                arguments=dict(progress.tool_call.arguments),
            )
            entry = self._agent_runtime.bindings.tool_snapshot.get(
                progress.tool_call.tool_name
            )
            common = {
                "envelope": progress_envelope,
                "tool_call": tool_call,
                "batch_id": progress.step_id,
                "call_index": progress.call_index,
                "total_calls": progress.total_calls,
                "tool_source": entry.source.value if entry is not None else None,
                "tool_origin": entry.origin if entry is not None else None,
                "hook_action": progress.tool_call.execution_policy,
                "confirmation": (
                    "pending"
                    if progress.tool_call.execution_policy == "confirm"
                    else "not_requested"
                ),
            }
            if isinstance(progress, ToolCallCompletedProgress):
                await self._events.emit(
                    ToolCallCompleted(
                        **common,
                        tool_result=progress.result,
                    )
                )
            elif isinstance(progress, ToolCallStartedProgress):
                await self._events.emit(ToolCallStarted(**common))

        try:
            result = await self._agent_runtime.drive_operation(
                operation_id,
                host_calls=self._runtime_bus.host_calls.client,
                consume_delta=consume_delta,
                consume_progress=consume_progress,
            )
            elapsed_ms = round((time.perf_counter() - started) * 1000)
            if result.assistant_message is not None:
                await self._events.emit(
                    AssistantMessageEvent(
                        envelope=envelope,
                        text=self._assistant_text(result.assistant_message),
                    )
                )
            await self._events.emit(
                AgentRunCompleted(
                    envelope=envelope,
                    elapsed_ms=elapsed_ms,
                    outcome=("blocked" if result.status == "waiting" else "completed"),
                )
            )
            timer.finish(attributes={"outcome": result.status})
            self._refresh_session()
            return AgentRunResult(
                status=("blocked" if result.status == "waiting" else "completed"),
                session_id=self._session.session_id,
                operation_id=operation_id,
                message=result.assistant_message,
                usage=None,
                elapsed_ms=elapsed_ms,
            )
        except asyncio.CancelledError:
            cancel = getattr(self._agent_runtime, "cancel_operation", None)
            if callable(cancel):
                cancel(operation_id, reason="用户中断")
            await self._events.emit(
                AgentRunInterrupted(envelope=envelope, at_step=0, partial_text="")
            )
            timer.finish(
                status="cancelled",
                attributes={"outcome": "cancelled"},
            )
            self._refresh_session()
            raise
        except Exception as exc:
            elapsed_ms = round((time.perf_counter() - started) * 1000)
            fail = getattr(self._agent_runtime, "fail_operation", None)
            if callable(fail):
                fail(
                    operation_id,
                    error_type=type(exc).__name__,
                    message=str(exc),
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
            async with self._control_lock:
                if self._active_task is task:
                    self._active_task = None
                    self._active_operation_id = None

    async def interrupt(self, *, expected_operation_id: str) -> None:
        async with self._control_lock:
            if self._active_operation_id != expected_operation_id:
                raise ValueError(
                    "活动 Operation 不匹配: "
                    f"expected={expected_operation_id}, "
                    f"actual={self._active_operation_id}"
                )
            assert self._active_task is not None
            self._active_task.cancel()

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
        thinking = version.model.provider_options.get("thinking")
        return RuntimeSnapshot(
            session_id=preview.session_id,
            agent_id=preview.agent_id,
            status=preview.status,
            message_count=preview.message_count,
            updated_at=preview.updated_at,
            last_message=preview.last_message,
            model_id=f"{version.model.provider}/{version.model.model}",
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
            for entry in self._agent_runtime.bindings.tool_snapshot.entries
        )

    async def inspect_context(self) -> ContextInspection:
        entries = self._conversation_service.list_active_branch_entries(
            session_id=self._session.session_id
        )
        context = ModelContextBuilder().build_model_context(
            agent_package_version=self._loaded_agent_package.version,
            conversation_entries=entries,
        )
        tool_calls = sum(
            1
            for message in context.messages
            for block in message.content
            if getattr(block, "type", None) == "tool_call"
        )
        version = self._loaded_agent_package.version
        return ContextInspection(
            usage=estimate_context_usage(
                context,
                model_label=f"{version.model.provider} / {version.model.model}",
                max_input_tokens=version.model.max_input_tokens,
            ),
            last_turn=None,
            session_total=None,
            note="本地字符估算；实际用量以 Provider 响应为准",
            turns=sum(1 for message in context.messages if message.role == "user"),
            tool_calls=tool_calls,
            compactions=sum(
                1
                for entry in entries
                if entry.object.object_type == "history_compaction"
            ),
            tool_definitions=len(context.tools),
        )

    def list_pending_skills(self) -> tuple[PendingSkillInfo, ...]:
        store = self._agent_runtime.bindings.tool_services.skill_store
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
        store = self._agent_runtime.bindings.tool_services.skill_store
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
        self._closed = True

    async def close(self) -> None:
        self.detach()

    def set_model(self, model_id: str):
        raise ValueError("模型切换需要创建新的 AgentPackageVersion 并重装 Runtime")

    def set_thinking(self, level: str) -> None:
        raise ValueError("thinking 切换需要创建新的 AgentPackageVersion 并重装 Runtime")

    def export_observation(self, out: Path | None = None) -> Path:
        self.flush()
        return export_operation_report(
            conversation_service=self._conversation_service,
            sessions=(self._session,),
            out=out,
        )

    def _open_trace(self) -> None:
        observability = getattr(self._app_config, "observability", None)
        configured = getattr(observability, "trace", None)
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
