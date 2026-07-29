"""面向 CLI、TUI 与后端的 Runtime Application Interface。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pickel.app.boot import Boot
from pickel.app.runtime_models import (
    AgentInfo,
    ContextInspection,
    ConversationClosedError,
    ModelInfo,
    PendingSkillInfo,
    ReloadResult,
    RuntimeSnapshot,
    SkillActionResult,
    ToolInfo,
    TurnInProgressError,
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
from pickel.runs.event_bus import EventBus
from pickel.runs.measure import measure
from pickel.runs.run import Run
from pickel.runs.runtime_events import RuntimeEventHandler
from pickel.runs.trace_sink import JsonlTraceSink, trace_enabled, trace_path
from pickel.runs.turn_usage import last_turn_usage, session_usage
from pickel.runs.usage_anchor import resolve_anchor
from pickel.shared.model_config import ModelSelection
from pickel.skills.store import SkillStoreError

if TYPE_CHECKING:
    from pickel.agents.agent import Agent

logger = logging.getLogger(__name__)


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
        trace_path_resolver: Callable[[str], Path] = trace_path,
        trace_sink_factory: Callable[[Path], JsonlTraceSink] = JsonlTraceSink,
    ) -> None:
        self._agent = agent
        self._run = run
        self._session = session
        self._session_service = session_service
        self._app_config = app_config
        self._trace_path_resolver = trace_path_resolver
        self._trace_sink_factory = trace_sink_factory
        self._bus = EventBus()
        self._closed = False
        self._turn_running = False
        self._trace_sink: JsonlTraceSink | None = None
        self._unsubscribe_trace: Callable[[], None] | None = None
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
    def event_bus(self) -> EventBus:
        return self._bus

    @property
    def trace_sink(self) -> JsonlTraceSink | None:
        return self._trace_sink

    @property
    def trace_path_resolver(self) -> Callable[[str], Path]:
        return self._trace_path_resolver

    @property
    def trace_sink_factory(self) -> Callable[[Path], JsonlTraceSink]:
        return self._trace_sink_factory

    @property
    def closed(self) -> bool:
        return self._closed

    async def turn(self, text: str) -> AssistantMessage:
        if self._closed:
            raise ConversationClosedError("Conversation 已关闭")
        if self._turn_running:
            raise TurnInProgressError("当前 Conversation 已有 turn 正在执行")
        self._turn_running = True
        try:
            return await self._run.turn(
                session=self._session,
                user_text=text,
                bus=self._bus,
            )
        finally:
            self._turn_running = False

    def subscribe(self, handler: RuntimeEventHandler) -> Callable[[], None]:
        return self._bus.subscribe(handler)

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

    def archive(self) -> None:
        self._close_trace()
        if self._closed:
            return
        if self._session_service is not None:
            self._session_service.close(session=self._session)
        self._closed = True

    def detach(self) -> None:
        """状态切换时只释放观察资源，不归档旧会话以保持现有 CLI 语义。"""
        self._close_trace()
        self._closed = True

    def _open_trace(self) -> None:
        if not trace_enabled(
            self._app_config.trace_enabled if self._app_config is not None else False
        ):
            return
        try:
            self._trace_sink = self._trace_sink_factory(
                self._trace_path_resolver(self._session.session_id)
            )
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

    def __init__(self, boot: Boot) -> None:
        self._boot = boot
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

    def start(self, agent_id: str | None = None) -> RuntimeConversation:
        agent, run = self._boot.build_run(agent_id=agent_id)
        service = self._boot.build_session_service(agent_id=agent.agent_id)
        session = service.start(
            agent_id=agent.agent_id,
            cwd=str(Path.cwd().resolve()),
        )
        run.session_service = service
        return self.attach(
            agent=agent,
            run=run,
            session=session,
            session_service=service,
        )

    def resume(self, session_id: str) -> RuntimeConversation:
        service = self._boot.build_session_service()
        session = service.resume(session_id=session_id)
        service = self._boot.build_session_service(agent_id=session.agent_id)
        agent, run = self._boot.build_run(
            agent_id=session.agent_id,
            session_service=service,
        )
        return self.attach(
            agent=agent,
            run=run,
            session=session,
            session_service=service,
        )

    def attach(
        self,
        *,
        agent: Agent,
        run: Run | Any,
        session: Session,
        session_service: SessionService | Any | None = None,
        trace_path_resolver: Callable[[str], Path] = trace_path,
        trace_sink_factory: Callable[[Path], JsonlTraceSink] = JsonlTraceSink,
    ) -> RuntimeConversation:
        return RuntimeConversation(
            agent=agent,
            run=run,
            session=session,
            session_service=session_service,
            app_config=self.app_config,
            trace_path_resolver=trace_path_resolver,
            trace_sink_factory=trace_sink_factory,
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
            trace_path_resolver=conversation.trace_path_resolver,
            trace_sink_factory=conversation.trace_sink_factory,
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
