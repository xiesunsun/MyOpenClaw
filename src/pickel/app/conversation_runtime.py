"""基于 AgentRuntime 与 ConversationSession 的应用控制面。"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

from pickel.agents.agent_package import LoadedAgentPackage
from pickel.app.runtime_models import (
    ContextInspection,
    ConversationClosedError,
    PendingSkillInfo,
    RuntimeErrorInfo,
    RuntimeSnapshot,
    SkillActionResult,
    ToolInfo,
    TurnInProgressError,
    TurnRequest,
    TurnResult,
)
from pickel.config.app_config import AppConfig
from pickel.context.model_context_builder import ModelContextBuilder
from pickel.conversations.agent_message import AssistantMessage
from pickel.conversations.content_blocks import TextContent
from pickel.conversations.conversation_service import ConversationService
from pickel.conversations.conversation_session import ConversationSession
from pickel.runs.conversation_output_bus import ConversationOutputBus
from pickel.runs.event_bus import EventBus
from pickel.runs.runtime_bus import RuntimeBus
from pickel.runs.runtime_events import (
    AssistantMessageEvent,
    RuntimeEventHandler,
    TextDeltaEvent,
    ThinkingDeltaEvent,
    ToolCallArgsDeltaEvent,
    TurnCompleted,
    TurnFailed,
    TurnStarted,
)
from pickel.providers.stream import (
    TextDelta,
    ThinkingDelta,
    ToolCallArgsDelta,
)
from pickel.runtime.agent_runtime import AgentRuntime
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
        app_config: AppConfig,
        mode: ConversationMode = "batch",
    ) -> None:
        self._loaded_agent_package = loaded_agent_package
        self._agent_runtime = agent_runtime
        self._session = session
        self._conversation_service = conversation_service
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

    @property
    def agent(self):
        return self._loaded_agent_package.agent

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

    async def turn(self, request: TurnRequest) -> TurnResult:
        if self._closed:
            raise ConversationClosedError("Conversation 已关闭")
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("turn 必须运行在 asyncio.Task 中")
        async with self._control_lock:
            if self._active_task is not None:
                raise TurnInProgressError("当前 Conversation 已有 Operation 正在执行")
            accepted = self._agent_runtime.accept_agent_run(
                session_id=self._session.session_id,
                user_message=request.message,
            )
            operation_id = accepted.operation.operation_id
            self._active_operation_id = operation_id
            self._active_task = task
        started = time.perf_counter()
        envelope = EventEnvelope(
            session_id=self._session.session_id,
            turn_id=operation_id,
        )
        await self._events.emit(
            TurnStarted(envelope=envelope, user_text=self._user_text(request))
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

        try:
            result = await self._agent_runtime.drive_operation(
                operation_id,
                host_calls=self._runtime_bus.host_calls.client,
                consume_delta=consume_delta,
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
                TurnCompleted(
                    envelope=envelope,
                    elapsed_ms=elapsed_ms,
                    outcome=("blocked" if result.status == "waiting" else "completed"),
                )
            )
            self._refresh_session()
            return TurnResult(
                status=("blocked" if result.status == "waiting" else "completed"),
                session_id=self._session.session_id,
                turn_id=operation_id,
                message=result.assistant_message,
                usage=None,
                elapsed_ms=elapsed_ms,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            elapsed_ms = round((time.perf_counter() - started) * 1000)
            await self._events.emit(
                TurnFailed(
                    envelope=envelope,
                    error_type=type(exc).__name__,
                    message=str(exc),
                )
            )
            return TurnResult(
                status="failed",
                session_id=self._session.session_id,
                turn_id=operation_id,
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
        context = await ModelContextBuilder().build_model_context(
            agent_package_version=self._loaded_agent_package.version,
            conversation_entries=entries,
            session_id=self._session.session_id,
            recall_sources=(),
        )
        tool_calls = sum(
            1
            for message in context.messages
            for block in message.content
            if getattr(block, "type", None) == "tool_call"
        )
        return ContextInspection(
            usage=None,
            last_turn=None,
            session_total=None,
            note="新 Runtime 尚未接入 token 计量投影",
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
        raise NotImplementedError("新 Runtime 的 Operation 观测导出将在事件迁移后提供")

    def _refresh_session(self) -> None:
        self._session = self._conversation_service.load_conversation_session(
            self._session.session_id
        )

    @staticmethod
    def _user_text(request: TurnRequest) -> str:
        return "\n".join(
            block.text
            for block in request.message.content
            if isinstance(block, TextContent)
        )

    @staticmethod
    def _assistant_text(message: AssistantMessage) -> str:
        return "\n".join(
            block.text for block in message.content if isinstance(block, TextContent)
        )
