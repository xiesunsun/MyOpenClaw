"""面向 CLI、TUI 与后端的 Runtime Application Interface。"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from collections.abc import Coroutine
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from pickel.app.boot import Boot
from pickel.app.host_call_recorder import SessionHostCallRecorder
from pickel.app.runtime_models import (
    AgentInfo,
    ContextInspection,
    ConversationClosedError,
    ConversationRequest,
    McpInspection,
    McpServerInfo,
    ModelInfo,
    NoActiveTurnError,
    PendingInputConflictError,
    PendingInputNotFoundError,
    PendingSkillInfo,
    ReloadResult,
    RuntimeErrorInfo,
    RuntimeSnapshot,
    SkillActionResult,
    ToolInfo,
    TurnInProgressError,
    TurnMismatchError,
    TurnRequest,
    TurnResult,
)
from pickel.config.app_config import AppConfig
from pickel.config.loader import Config
from pickel.context.prepare import prepare
from pickel.conversations.agent_message import (
    AssistantMessage,
    UserMessage,
    agent_message_from_dict,
)
from pickel.conversations.content_blocks import ToolCallContent
from pickel.conversations.service import SessionService
from pickel.conversations.session import Session
from pickel.conversations.session_entry import (
    ENTRY_TYPE_COMPACTION,
    ENTRY_TYPE_MESSAGE,
)
from pickel.conversations.session_storage_mapper import build_session_preview
from pickel.extensions_host.loader import (
    LoadResult,
    load_extensions_async,
    teardown_extensions,
)
from pickel.extensions_host.event_processor import (
    ConversationExtensionContext,
    EventProcessor,
)
from pickel.runs.event_bus import EventBus
from pickel.runs.conversation_output_bus import ConversationOutputBus
from pickel.runs.measure import measure
from pickel.runs.run import Run
from pickel.runs.runtime_bus import RuntimeBus
from pickel.runs.runtime_events import (
    PendingInputCancelled,
    PendingInputQueued,
    PendingInputUpdated,
    RuntimeEventHandler,
    TurnCompleted,
    TurnFailed,
    TurnInterrupted,
    TurnStarted,
)
from pickel.runs.trace_sink import (
    JsonlTraceSink,
    TraceOptions,
    trace_mode,
    trace_path,
)
from pickel.runs.turn_mailbox import (
    PendingInput,
    TurnMailbox,
    TurnMailboxClosedError,
)
from pickel.runs.turn_mailbox import (
    PendingInputConflictError as MailboxPendingInputConflictError,
)
from pickel.runs.turn_usage import last_turn_usage, session_usage
from pickel.runs.usage_anchor import resolve_anchor
from pickel.shared.event_envelope import EventEnvelope
from pickel.shared.conversation_output import (
    ConversationOutputBase,
    ConversationOutputHandler,
)
from pickel.shared.conversation_mode import ConversationMode
from pickel.shared.model_config import ModelSelection
from pickel.skills.store import SkillStoreError

if TYPE_CHECKING:
    from pickel.agents.agent import Agent

logger = logging.getLogger(__name__)


@dataclass
class _TurnExecution:
    """RuntimeConversation 私有的活动 turn 资源句柄。"""

    turn_id: str
    task: asyncio.Task[Any]
    mailbox: TurnMailbox


@dataclass
class _EventProcessorBinding:
    """RuntimeConversation 私有的 extension 事件处理器。"""

    processor: EventProcessor
    unsubscribe: Callable[[], None]


class RuntimeConversation:
    """一个活动会话及其运行资源。

    界面只订阅事件和调用这里的能力，不需要认识 Run、SessionService 或 ToolBus。
    """

    def __init__(
        self,
        *,
        agent: Agent,
        run: Run | Any,
        session: Session,
        session_service: SessionService | Any | None = None,
        app_config: AppConfig | None = None,
        mode: ConversationMode = "batch",
        trace_path_resolver: Callable[[str], Path] = trace_path,
        trace_sink_factory: Callable[..., JsonlTraceSink] = JsonlTraceSink,
    ) -> None:
        self._agent = agent
        self._run = run
        self._session = session
        self._session_service = session_service
        self._app_config = app_config
        self._mode = mode
        self._trace_path_resolver = trace_path_resolver
        self._trace_sink_factory = trace_sink_factory
        self._runtime_bus = RuntimeBus(
            host_call_recorder=SessionHostCallRecorder(
                session=session,
                session_service=session_service,
            )
        )
        self._bus = self._runtime_bus.events
        self._outputs = ConversationOutputBus()
        self._closed = False
        self._execution: _TurnExecution | None = None
        self._follow_ups: list[PendingInput] = []
        self._control_lock = asyncio.Lock()
        self._trace_sink: JsonlTraceSink | None = None
        self._unsubscribe_trace: Callable[[], None] | None = None
        self._event_processors: list[_EventProcessorBinding] = []
        self._background_tasks: set[asyncio.Task[None]] = set()
        self._open_trace()

    @property
    def agent(self) -> Agent:
        return self._agent

    @property
    def session(self) -> Session:
        return self._session

    @property
    def session_service(self) -> SessionService | Any | None:
        return self._session_service

    @property
    def app_config(self) -> AppConfig | None:
        return self._app_config

    @property
    def mode(self) -> ConversationMode:
        return self._mode

    @property
    def event_bus(self) -> EventBus:
        return self._bus

    @property
    def runtime_bus(self) -> RuntimeBus:
        return self._runtime_bus

    @property
    def trace_sink(self) -> JsonlTraceSink | None:
        return self._trace_sink

    @property
    def trace_path_resolver(self) -> Callable[[str], Path]:
        return self._trace_path_resolver

    @property
    def trace_sink_factory(self) -> Callable[..., JsonlTraceSink]:
        return self._trace_sink_factory

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def active_turn_id(self) -> str | None:
        execution = self._execution
        return execution.turn_id if execution is not None else None

    async def turn(self, request: TurnRequest) -> TurnResult:
        if self._closed:
            raise ConversationClosedError("Conversation 已关闭")
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("turn 必须运行在 asyncio.Task 中")
        turn_id = str(uuid4())
        async with self._control_lock:
            if self._execution is not None:
                raise TurnInProgressError("当前 Conversation 已有 turn 正在执行")
            self._execution = _TurnExecution(
                turn_id=turn_id,
                task=task,
                mailbox=TurnMailbox(turn_id),
            )

        current_request = request
        pending_input: PendingInput | None = None
        last_result: TurnResult | None = None
        try:
            while True:
                execution = self._execution
                assert execution is not None
                last_result = await self._execute_turn(
                    current_request,
                    turn_id=execution.turn_id,
                    mailbox=execution.mailbox,
                    pending_input=pending_input,
                )

                cancelled: tuple[PendingInput, ...] = ()
                async with self._control_lock:
                    if last_result.status != "completed":
                        cancelled = (
                            *await execution.mailbox.close_and_drain(),
                            *self._follow_ups,
                        )
                        self._follow_ups.clear()
                        self._execution = None
                    elif not self._follow_ups:
                        cancelled = await execution.mailbox.close_and_drain()
                        self._execution = None
                    else:
                        pending_input = self._follow_ups.pop(0)
                        next_turn_id = str(uuid4())
                        self._execution = _TurnExecution(
                            turn_id=next_turn_id,
                            task=task,
                            mailbox=TurnMailbox(next_turn_id),
                        )
                        current_request = TurnRequest(message=pending_input.message)
                        continue
                for item in cancelled:
                    await self._emit_pending_event(PendingInputCancelled, item)
                return last_result
        finally:
            cancelled: tuple[PendingInput, ...] = ()
            async with self._control_lock:
                if self._execution is not None and self._execution.task is task:
                    cancelled = (
                        *await self._execution.mailbox.close_and_drain(),
                        *self._follow_ups,
                    )
                    self._follow_ups.clear()
                    self._execution = None
            for item in cancelled:
                await self._emit_pending_event(PendingInputCancelled, item)

    async def _execute_turn(
        self,
        request: TurnRequest,
        *,
        turn_id: str,
        mailbox: TurnMailbox,
        pending_input: PendingInput | None,
    ) -> TurnResult:
        started = time.perf_counter()
        outcome = "completed"
        usage = None
        elapsed_ms = 0
        interrupted_seen = False

        def capture(event) -> None:
            nonlocal turn_id, outcome, usage, elapsed_ms, interrupted_seen
            if isinstance(event, TurnStarted):
                turn_id = event.envelope.turn_id
            elif isinstance(event, TurnCompleted):
                turn_id = event.envelope.turn_id
                outcome = event.outcome
                usage = event.usage
                elapsed_ms = event.elapsed_ms
            elif isinstance(event, TurnFailed):
                turn_id = event.envelope.turn_id
            elif isinstance(event, TurnInterrupted):
                interrupted_seen = True

        unsubscribe = self._bus.subscribe(capture)
        try:
            kwargs = {
                "session": self._session,
                "user_message": request.message,
                "bus": self._bus,
            }
            if isinstance(self._run, Run):
                kwargs["observer"] = self._trace_sink
                kwargs["host_calls"] = self._runtime_bus.host_calls.client
                kwargs["turn_id"] = turn_id
                kwargs["turn_input"] = mailbox
                kwargs["pending_input"] = pending_input
            try:
                reply = await self._run.turn(**kwargs)
            except asyncio.CancelledError:
                if not interrupted_seen:
                    await self._bus.emit(
                        TurnInterrupted(
                            envelope=EventEnvelope(
                                session_id=self._session.session_id,
                                turn_id=turn_id,
                            ),
                            at_step=0,
                            partial_text="",
                        )
                    )
                raise
            except Exception as exc:  # noqa: BLE001 — Application 返回稳定失败结果
                return TurnResult(
                    status="failed",
                    session_id=self._session.session_id,
                    turn_id=turn_id,
                    message=None,
                    usage=usage,
                    elapsed_ms=elapsed_ms
                    or round((time.perf_counter() - started) * 1000),
                    error=RuntimeErrorInfo(
                        error_type=type(exc).__name__,
                        message=str(exc),
                    ),
                )
            return TurnResult(
                status="blocked" if outcome == "blocked" else "completed",
                session_id=self._session.session_id,
                turn_id=turn_id,
                message=reply,
                usage=usage,
                elapsed_ms=elapsed_ms or round((time.perf_counter() - started) * 1000),
            )
        finally:
            unsubscribe()

    async def steer(
        self,
        request: TurnRequest,
        *,
        expected_turn_id: str,
    ) -> PendingInput:
        async with self._control_lock:
            execution = self._require_execution(expected_turn_id)
            try:
                item = await execution.mailbox.add(request.message)
            except TurnMailboxClosedError as exc:
                raise TurnInProgressError(str(exc)) from exc
        await self._emit_pending_event(PendingInputQueued, item)
        return item

    async def follow_up(
        self,
        request: TurnRequest,
        *,
        expected_turn_id: str,
    ) -> PendingInput:
        async with self._control_lock:
            self._require_execution(expected_turn_id)
            item = PendingInput.create(
                message=request.message,
                delivery="follow_up",
                target_turn_id=expected_turn_id,
            )
            self._follow_ups.append(item)
        await self._emit_pending_event(PendingInputQueued, item)
        return item

    async def update_pending(
        self,
        input_id: str,
        request: TurnRequest,
        *,
        expected_revision: int,
    ) -> PendingInput:
        async with self._control_lock:
            execution = self._execution
            if execution is not None:
                try:
                    updated = await execution.mailbox.update(
                        input_id,
                        request.message,
                        expected_revision=expected_revision,
                    )
                except MailboxPendingInputConflictError as exc:
                    raise PendingInputConflictError(str(exc)) from exc
                if updated is not None:
                    item = updated
                else:
                    item = self._update_follow_up(
                        input_id,
                        request.message,
                        expected_revision=expected_revision,
                    )
            else:
                item = self._update_follow_up(
                    input_id,
                    request.message,
                    expected_revision=expected_revision,
                )
        await self._emit_pending_event(PendingInputUpdated, item)
        return item

    async def cancel_pending(
        self,
        input_id: str,
        *,
        expected_revision: int,
    ) -> bool:
        async with self._control_lock:
            execution = self._execution
            if execution is not None:
                try:
                    removed = await execution.mailbox.cancel(
                        input_id,
                        expected_revision=expected_revision,
                    )
                except MailboxPendingInputConflictError as exc:
                    raise PendingInputConflictError(str(exc)) from exc
                if removed is None:
                    removed = self._cancel_follow_up(
                        input_id,
                        expected_revision=expected_revision,
                    )
            else:
                removed = self._cancel_follow_up(
                    input_id,
                    expected_revision=expected_revision,
                )
        await self._emit_pending_event(PendingInputCancelled, removed)
        return True

    async def pending_inputs(self) -> tuple[PendingInput, ...]:
        async with self._control_lock:
            steering = (
                await self._execution.mailbox.snapshot()
                if self._execution is not None
                else ()
            )
            return (*steering, *self._follow_ups)

    async def interrupt(
        self,
        *,
        expected_turn_id: str,
    ) -> tuple[PendingInput, ...]:
        async with self._control_lock:
            execution = self._require_execution(expected_turn_id)
            steering = await execution.mailbox.close_and_drain()
            returned = (*steering, *self._follow_ups)
            self._follow_ups.clear()
            execution.task.cancel()
            return returned

    def _require_execution(self, expected_turn_id: str) -> _TurnExecution:
        execution = self._execution
        if execution is None:
            raise NoActiveTurnError("当前 Conversation 没有正在执行的 turn")
        if execution.turn_id != expected_turn_id:
            raise TurnMismatchError(
                f"活动 turn 不匹配：expected={expected_turn_id}, "
                f"actual={execution.turn_id}"
            )
        return execution

    def _update_follow_up(
        self,
        input_id: str,
        message: UserMessage,
        *,
        expected_revision: int,
    ) -> PendingInput:
        for index, item in enumerate(self._follow_ups):
            if item.input_id != input_id:
                continue
            if item.revision != expected_revision:
                raise PendingInputConflictError(
                    f"待执行输入版本不匹配：expected={expected_revision}, "
                    f"actual={item.revision}"
                )
            updated = replace(
                item,
                content=tuple(message.content),
                revision=item.revision + 1,
            )
            self._follow_ups[index] = updated
            return updated
        raise PendingInputNotFoundError(f"待执行输入不存在：{input_id}")

    def _cancel_follow_up(
        self,
        input_id: str,
        *,
        expected_revision: int,
    ) -> PendingInput:
        for index, item in enumerate(self._follow_ups):
            if item.input_id != input_id:
                continue
            if item.revision != expected_revision:
                raise PendingInputConflictError(
                    f"待执行输入版本不匹配：expected={expected_revision}, "
                    f"actual={item.revision}"
                )
            return self._follow_ups.pop(index)
        raise PendingInputNotFoundError(f"待执行输入不存在：{input_id}")

    async def _emit_pending_event(self, event_type, item: PendingInput) -> None:
        await self._bus.emit(
            event_type(
                envelope=EventEnvelope(
                    session_id=self._session.session_id,
                    turn_id=item.target_turn_id,
                ),
                input_id=item.input_id,
                delivery=item.delivery,
                target_turn_id=item.target_turn_id,
                revision=item.revision,
            )
        )

    def subscribe(self, handler: RuntimeEventHandler) -> Callable[[], None]:
        return self._bus.subscribe(handler)

    def subscribe_outputs(
        self,
        handler: ConversationOutputHandler,
    ) -> Callable[[], None]:
        return self._outputs.subscribe(handler)

    async def publish_output(self, output: ConversationOutputBase) -> None:
        await self._outputs.publish(output)

    def start_background_task(
        self,
        coroutine: Coroutine[Any, Any, None],
        name: str,
    ) -> None:
        """启动由 Conversation 统一取消和回收的后台任务。"""
        if self._closed:
            coroutine.close()
            raise ConversationClosedError("Conversation 已关闭")
        task = asyncio.create_task(coroutine, name=name)
        self._background_tasks.add(task)
        task.add_done_callback(self._finish_background_task)

    def _finish_background_task(self, task: asyncio.Task[None]) -> None:
        self._background_tasks.discard(task)
        if task.cancelled():
            return
        try:
            error = task.exception()
        except asyncio.CancelledError:
            return
        if error is not None:
            logger.error(
                "Conversation 后台任务失败: %s",
                task.get_name(),
                exc_info=(type(error), error, error.__traceback__),
            )

    def add_event_processor(
        self,
        processor: EventProcessor,
        event_types: tuple[type[Any], ...],
    ) -> None:
        """把一个会话级 extension 处理器挂到只读事件流。"""

        async def handle(event) -> None:
            if isinstance(event, event_types):
                await processor.handle_event(event)

        self._event_processors.append(
            _EventProcessorBinding(
                processor=processor,
                unsubscribe=self.subscribe(handle),
            )
        )

    def snapshot(self) -> RuntimeSnapshot:
        preview = (
            self._session_service.build_preview(session=self._session)
            if self._session_service is not None
            else build_session_preview(session=self._session)
        )
        model = self._agent.model_config
        thinking = getattr(self._run, "environ", None)
        thinking_value = (
            thinking.provider_options.get("thinking") if thinking is not None else None
        )
        return RuntimeSnapshot(
            session_id=preview.session_id,
            agent_id=preview.agent_id,
            status=preview.status,
            message_count=preview.message_count,
            updated_at=preview.updated_at,
            last_message=preview.last_message,
            model_id=f"{model.provider}/{model.model}",
            thinking=thinking_value,
        )

    def list_tools(self) -> tuple[ToolInfo, ...]:
        snapshot = self._run.tool_bus.snapshot(self._run.activation)
        return tuple(
            ToolInfo(
                name=entry.name,
                source=entry.source.value,
                origin=entry.origin,
                version=entry.version,
            )
            for entry in sorted(snapshot.entries, key=lambda item: item.name)
        )

    def list_pending_skills(self) -> tuple[PendingSkillInfo, ...]:
        store = getattr(self._run, "skill_store", None)
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

    def apply_skill_action(
        self,
        action: str,
        pending_id: str,
    ) -> SkillActionResult:
        store = getattr(self._run, "skill_store", None)
        if store is None:
            raise SkillStoreError("当前 agent 未配置 skills 目录")
        if action == "diff":
            return SkillActionResult(
                action=action,
                pending_id=pending_id,
                diff=store.diff(pending_id),
            )
        if action == "approve":
            return SkillActionResult(
                action=action,
                pending_id=pending_id,
                path=store.approve(pending_id),
            )
        if action == "reject":
            store.reject(pending_id)
            return SkillActionResult(action=action, pending_id=pending_id)
        raise SkillStoreError(f"未知 skill 操作：{action}")

    def set_model(self, model_id: str) -> ModelInfo:
        if self._app_config is None:
            raise ValueError("AppConfig 未提供，无法设置 model")
        selection = _parse_model(self._app_config, model_id)
        self._run.environ.llm = selection
        self._run.apply_environ_model(self._app_config)
        return ModelInfo(provider=selection.provider, model=selection.model)

    def set_thinking(self, level: str) -> None:
        if self._app_config is None:
            raise ValueError("AppConfig 未提供，无法设置 thinking")
        self._run.environ.provider_options["thinking"] = level
        self._run.apply_environ_model(self._app_config)

    async def inspect_context(self) -> ContextInspection:
        last_turn = last_turn_usage(self._session)
        total = session_usage(self._session)
        turns, tool_calls, compactions = _session_context_stats(self._session)
        usage = None
        note = None
        tool_defs = 0
        try:
            snapshot = self._run.tool_bus.snapshot(self._run.activation)
            tool_defs = len(snapshot.entries)
            request = await prepare(
                run=self._run,
                session=self._session,
                hook_feedback=[],
                unit_window=self._run.unit_window,
                recall_sources=[],
                snapshot=snapshot,
            )
            model_config = self._run.agent.model_config
            usage = await measure(
                request=request,
                anchor=resolve_anchor(
                    session=self._session,
                    request=request,
                    provider=model_config.provider,
                    model=model_config.model,
                ),
                provider=self._run.provider,
                model_config=model_config,
            )
        except Exception as exc:  # 不完整的测试 Run 也应返回可展示结果
            note = f"组装失败: {exc}"
        if last_turn is None and note is None:
            note = "本会话尚未成功完成过模型调用（无 API usage）"
        return ContextInspection(
            usage=usage,
            last_turn=last_turn,
            session_total=total if total is not None and total.steps > 1 else None,
            note=note,
            turns=turns,
            tool_calls=tool_calls,
            compactions=compactions,
            tool_definitions=tool_defs,
        )

    def flush(self) -> None:
        if self._session_service is not None:
            self._session_service.flush_new_entries(
                session=self._session,
                entries=[],
            )

    def export_observation(self, out: Path | None = None) -> Path:
        """导出当前会话的观测 HTML，供 CLI、TUI 与后端共同调用。"""
        from pickel.observe.exporter import export_html

        self.flush()
        flush_trace = getattr(self._trace_sink, "flush", None)
        if callable(flush_trace):
            flush_trace()
        return export_html(
            [self._session],
            out=out,
            trace_path_resolver=self._trace_path_resolver,
        )

    def archive(self) -> None:
        self._close_trace()
        if self._closed:
            return
        self._close_event_processors()
        self._close_bash()
        self._runtime_bus.close_now()
        if self._session_service is not None:
            self._session_service.close(session=self._session)
        self._closed = True

    def detach(self) -> None:
        """状态切换时只释放观察资源，不归档旧会话以保持现有 CLI 语义。"""
        self._close_trace()
        self._close_event_processors()
        self._close_bash()
        self._runtime_bus.close_now()
        self._closed = True

    def _close_bash(self) -> None:
        bash = getattr(self._run, "bash_operations", None)
        if bash is not None:
            bash.close(self._session.session_id)

    def _close_event_processors(self) -> None:
        for binding in reversed(self._event_processors):
            binding.unsubscribe()
        for task in tuple(self._background_tasks):
            task.cancel()
        self._background_tasks.clear()
        for binding in reversed(self._event_processors):
            try:
                binding.processor.close()
            except Exception:
                logger.exception("Extension event processor close failed")
        self._event_processors.clear()
        self._outputs.clear()

    def _open_trace(self) -> None:
        configured = (
            self._app_config.observability.trace
            if self._app_config is not None
            else None
        )
        mode = trace_mode(configured.mode if configured is not None else "standard")
        if mode == "off":
            return
        try:
            path = self._trace_path_resolver(self._session.session_id)
            option_values = {}
            if configured is not None:
                option_values = {
                    "queue_capacity": configured.queue_capacity,
                    "batch_size": configured.batch_size,
                    "flush_interval_ms": configured.flush_interval_ms,
                    "max_file_size_mb": configured.max_file_size_mb,
                    "max_age_days": configured.max_age_days,
                    "max_total_size_mb": configured.max_total_size_mb,
                }
            options = TraceOptions(mode=mode, **option_values)
            try:
                self._trace_sink = self._trace_sink_factory(path, options)
            except TypeError:
                # 兼容嵌入方和测试中的单参数 factory。
                self._trace_sink = self._trace_sink_factory(path)
        except OSError as exc:
            logger.warning("trace 打开失败，本次运行禁用 trace: %s", exc)
            return
        self._unsubscribe_trace = self._bus.subscribe(self._trace_sink)

    def _close_trace(self) -> None:
        if self._unsubscribe_trace is not None:
            self._unsubscribe_trace()
            self._unsubscribe_trace = None
        if self._trace_sink is not None:
            self._trace_sink.close()
            self._trace_sink = None


class RuntimeHost:
    """进程级 Runtime 应用入口。"""

    def __init__(
        self,
        boot: Boot,
        *,
        launch_agent_ids: tuple[str, ...] | None = None,
    ) -> None:
        self._boot = boot
        self._launch_agent_ids = launch_agent_ids
        self._extension_result = getattr(boot, "extension_result", None) or LoadResult()

    @property
    def boot(self) -> Boot:
        return self._boot

    @property
    def app_config(self) -> AppConfig:
        return self._boot.app_config

    def list_agents(self) -> tuple[AgentInfo, ...]:
        return tuple(
            AgentInfo(agent_id=item) for item in sorted(self.app_config.agents)
        )

    def list_models(self) -> tuple[ModelInfo, ...]:
        return tuple(
            ModelInfo(provider=provider, model=model)
            for provider in sorted(self.app_config.providers)
            for model in sorted(self.app_config.providers[provider].models)
        )

    def inspect_mcp(self, conversation: RuntimeConversation) -> McpInspection:
        """读取 MCP 最后已知状态，并按当前 Conversation 计算 active tools。"""
        source = self._boot.extensions.mcp_status_source
        if source is None:
            return McpInspection(available=False)

        snapshot = source.snapshot()
        active_by_server: dict[str, int] = {}
        for tool in conversation.list_tools():
            if tool.source != "mcp" or tool.origin is None:
                continue
            active_by_server[tool.origin] = active_by_server.get(tool.origin, 0) + 1

        return McpInspection(
            available=True,
            servers=tuple(
                McpServerInfo(
                    name=server.name,
                    status=server.status,
                    transport=server.transport,
                    config_scope=server.config_scope,
                    protocol_version=server.protocol_version,
                    implementation=_implementation_label(
                        server.implementation_name,
                        server.implementation_version,
                    ),
                    discovered_tools=server.discovered_tools,
                    active_tools=active_by_server.get(server.name, 0),
                    last_error=server.last_error,
                )
                for server in snapshot.servers
            ),
            diagnostics=snapshot.diagnostics,
        )

    def open_conversation(self, request: ConversationRequest) -> RuntimeConversation:
        cwd = str((request.cwd or Path.cwd()).resolve())
        if request.session_id is not None:
            lookup_service = self._boot.build_session_service()
            session = lookup_service.resume(session_id=request.session_id)
            service = self._boot.build_session_service(agent_id=session.agent_id)
            agent, run = self._boot.build_run(
                agent_id=session.agent_id,
                session_service=service,
            )
        else:
            agent, run = self._boot.build_run(agent_id=request.agent_id)
            if request.persistence == "persistent":
                service = self._boot.build_session_service(agent_id=agent.agent_id)
                session = service.start(agent_id=agent.agent_id, cwd=cwd)
                run.session_service = service
            else:
                service = None
                session = Session.create(agent_id=agent.agent_id, cwd=cwd)
        return self.attach(
            agent=agent,
            run=run,
            session=session,
            session_service=service,
            mode=request.mode,
        )

    def attach(
        self,
        *,
        agent: Agent,
        run: Run | Any,
        session: Session,
        session_service: SessionService | Any | None = None,
        mode: ConversationMode = "batch",
        trace_path_resolver: Callable[[str], Path] = trace_path,
        trace_sink_factory: Callable[..., JsonlTraceSink] = JsonlTraceSink,
    ) -> RuntimeConversation:
        conversation = RuntimeConversation(
            agent=agent,
            run=run,
            session=session,
            session_service=session_service,
            app_config=self.app_config,
            mode=mode,
            trace_path_resolver=trace_path_resolver,
            trace_sink_factory=trace_sink_factory,
        )
        self._add_event_processors(
            conversation,
            registry=self._boot.extensions,
        )
        return conversation

    @staticmethod
    def _add_event_processors(
        conversation: RuntimeConversation,
        *,
        registry,
    ) -> None:
        context = ConversationExtensionContext(
            agent_id=conversation.agent.agent_id,
            session_id=conversation.session.session_id,
            mode=conversation.mode,
            publish_output=conversation.publish_output,
            start_background_task=conversation.start_background_task,
        )
        for resolved in registry.resolve_event_processors(context):
            conversation.add_event_processor(
                resolved.processor,
                resolved.event_types,
            )

    def new_session(
        self,
        conversation: RuntimeConversation,
    ) -> RuntimeConversation:
        service = conversation.session_service
        session = (
            service.start(
                agent_id=conversation.agent.agent_id,
                cwd=str(Path.cwd().resolve()),
            )
            if service is not None
            else Session.create(
                agent_id=conversation.agent.agent_id,
                cwd=str(Path.cwd().resolve()),
            )
        )
        conversation.detach()
        return self.attach(
            agent=conversation.agent,
            run=conversation._run,
            session=session,
            session_service=service,
            mode=conversation.mode,
            trace_path_resolver=conversation.trace_path_resolver,
            trace_sink_factory=conversation.trace_sink_factory,
        )

    def switch_agent(
        self,
        conversation: RuntimeConversation,
        agent_id: str,
    ) -> RuntimeConversation:
        if agent_id not in self.app_config.agents:
            raise KeyError(f"Unknown agent: {agent_id}")
        service = self._boot.build_session_service(agent_id=agent_id)
        session = service.start(agent_id=agent_id, cwd=str(Path.cwd().resolve()))
        agent, run = self._boot.build_run(
            agent_id=agent_id,
            session_service=service,
        )
        conversation.detach()
        return self.attach(
            agent=agent,
            run=run,
            session=session,
            session_service=service,
            mode=conversation.mode,
            trace_path_resolver=conversation.trace_path_resolver,
            trace_sink_factory=conversation.trace_sink_factory,
        )

    async def reload(
        self,
        conversation: RuntimeConversation,
        *,
        app_config: AppConfig | None = None,
        boot_factory: Callable[..., Boot] = Boot.from_config,
    ) -> ReloadResult:
        """重建配置与 Run；失败时不交换 Host/Conversation。

        Extension 的外部进程具备自身生命周期，目前沿用既有 teardown/setup
        语义；Host 与 Conversation 的引用只在完整构造成功后交换。
        """
        app_config = app_config or Config.load(cwd=Path.cwd())
        tool_bus = self._boot.tool_bus
        await teardown_extensions(self._extension_result, tool_bus=tool_bus)
        extension_result = await load_extensions_async(
            tool_bus=tool_bus,
            app_config=app_config,
            enabled_names=app_config.resolve_agent_extensions(self._launch_agent_ids),
        )
        next_boot = boot_factory(
            app_config,
            tool_bus=tool_bus,
            extensions=extension_result.registry,
        )
        next_boot.extension_result = extension_result
        service = conversation.session_service or next_boot.build_session_service(
            agent_id=conversation.agent.agent_id
        )
        agent, run = Run.reload(
            boot=next_boot,
            old_run=conversation._run,
            agent_id=conversation.agent.agent_id,
            session_service=service,
        )
        next_conversation = RuntimeConversation(
            agent=agent,
            run=run,
            session=conversation.session,
            session_service=service,
            app_config=app_config,
            mode=conversation.mode,
            trace_path_resolver=conversation.trace_path_resolver,
            trace_sink_factory=conversation.trace_sink_factory,
        )
        self._add_event_processors(
            next_conversation,
            registry=next_boot.extensions,
        )
        conversation.detach()
        self._boot = next_boot
        self._extension_result = extension_result
        return ReloadResult(
            conversation=next_conversation,
            warnings=tuple(str(item) for item in extension_result.errors),
        )

    async def shutdown(self) -> None:
        await teardown_extensions(
            self._extension_result,
            tool_bus=self._boot.tool_bus,
        )


def _implementation_label(name: str | None, version: str | None) -> str | None:
    if name and version:
        return f"{name} {version}"
    return name or version


def _parse_model(app_config: AppConfig, arg: str) -> ModelSelection:
    providers = app_config.providers
    for provider_id in sorted(providers, key=len, reverse=True):
        prefix = f"{provider_id}/"
        if arg.startswith(prefix):
            model = arg[len(prefix) :]
            if model in providers[provider_id].models:
                return ModelSelection(provider=provider_id, model=model)
            raise KeyError(f"Unknown model '{model}' for provider '{provider_id}'")
    matches = [
        ModelSelection(provider=provider_id, model=arg)
        for provider_id, catalog in providers.items()
        if arg in catalog.models
    ]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        options = ", ".join(f"{item.provider}/{item.model}" for item in matches)
        raise ValueError(f"model '{arg}' 不唯一，请指定 provider/model：{options}")
    raise KeyError(f"Unknown model selection: {arg}")


def _session_context_stats(session: Session) -> tuple[int, int, int]:
    turns = 0
    tool_calls = 0
    compactions = 0
    for entry in session.active_path():
        if entry.entry_type == ENTRY_TYPE_COMPACTION:
            compactions += 1
            continue
        if entry.entry_type != ENTRY_TYPE_MESSAGE:
            continue
        try:
            message = agent_message_from_dict(entry.payload)
        except (KeyError, TypeError, ValueError):
            continue
        if isinstance(message, UserMessage):
            turns += 1
        elif isinstance(message, AssistantMessage):
            tool_calls += sum(
                isinstance(block, ToolCallContent) for block in message.content
            )
    return turns, tool_calls, compactions
