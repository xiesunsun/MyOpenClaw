"""Run：运行资源袋 + turn 边界。"""

from __future__ import annotations

import asyncio
import time
import traceback
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from uuid import uuid4

from pickel.agents.agent import Agent
from pickel.config.environ import Environ
from pickel.context.assembler import ContextAssembler
from pickel.context.hook_feedback import HookFeedback
from pickel.conversations.agent_message import AssistantMessage, UserMessage
from pickel.conversations.content_blocks import TextContent
from pickel.conversations.service import SessionService
from pickel.conversations.session import Session
from pickel.hooks.events import TurnEndEvent, UserPromptSubmitEvent
from pickel.hooks.lifecycle import LifecycleHooks, NoopLifecycleHooks
from pickel.observe.records import (
    ErrorInfo,
    ObservationIdentity,
    Observer,
    SpanTimer,
    observation_scope,
    span_scope,
)
from pickel.providers import create_llm_provider
from pickel.providers.base import Provider
from pickel.runs.runtime_events import TurnCompleted, TurnFailed, TurnStarted
from pickel.runs.turn_usage import last_turn_usage
from pickel.shared.event_envelope import EventEnvelope
from pickel.shared.file_access import FileAccessMode
from pickel.shared.model_config import ModelSelection
from pickel.skills.store import SkillStore
from pickel.tools.base import BaseTool, ToolExecutionContext
from pickel.tools.bus import ToolActivation, ToolBus, bus_with
from pickel.tools.file_service import WorkspaceFileService
from pickel.tools.policy import (
    FileAccessPolicy,
    FullAccessPathPolicy,
    WorkspacePathAccessPolicy,
)
from pickel.tools.services import ToolServices
from pickel.tools.shell import BashOperations, LocalBashOperations, ShellSessionManager

if TYPE_CHECKING:
    from pickel.app.boot import Boot
    from pickel.config.app_config import AppConfig
    from pickel.runs.event_bus import EventBus
    from pickel.runs.host_calls import HostCallClient
    from pickel.runs.strategy.base import ExecutionStrategy


@dataclass
class Run:
    """单次/会话级运行资源；open 构造，turn 执行用户一轮输入。"""

    agent: Agent
    provider: Provider
    tool_bus: ToolBus
    activation: ToolActivation
    context_assembler: ContextAssembler
    lifecycle_hooks: LifecycleHooks
    session_service: SessionService | None
    file_access_policy: FileAccessPolicy | None
    workspace_files: WorkspaceFileService | None
    shell_session_manager: ShellSessionManager
    unit_window: int
    strategy: ExecutionStrategy
    environ: Environ = field(default_factory=Environ)
    recall_sources: list = field(default_factory=list)
    skill_store: SkillStore | None = None
    bash_operations: BashOperations | None = None

    @classmethod
    def open(
        cls,
        agent: Agent,
        *,
        strategy: ExecutionStrategy | None = None,
        session_service: SessionService | None = None,
        unit_window: int = 5,
        context_assembler: ContextAssembler | None = None,
        lifecycle_hooks: LifecycleHooks | None = None,
        file_access_policy: FileAccessPolicy | None = None,
        shell_session_manager: ShellSessionManager | None = None,
        bash_operations: BashOperations | None = None,
        skill_store: SkillStore | None = None,
        provider: Provider | None = None,
        tools: list[BaseTool] | None = None,
        tool_bus: ToolBus | None = None,
        recall_sources: list | None = None,
    ) -> Run:
        """解析 provider/tools/workspace，组装 Run。"""
        from pickel.runs.strategy.react import ReActStrategy

        resolved_provider = provider or create_llm_provider(agent.model_config)
        # tool_bus 优先；只给 tools 时建一个私有 bus 并全量允许（测试便捷路径）
        if tool_bus is not None:
            resolved_bus = tool_bus
            activation = ToolActivation(allowed=frozenset(agent.tool_ids))
        else:
            resolved_bus = bus_with(tools or [])
            activation = ToolActivation(allowed=frozenset(resolved_bus.list_names()))

        resolved_policy = file_access_policy or cls._policy_for_mode(
            agent.file_access_mode
        )
        workspace_files = WorkspaceFileService(
            workspace_root=agent.workspace,
            access_policy=resolved_policy,
        )
        resolved_shell_sessions = shell_session_manager or ShellSessionManager()
        resolved_bash = bash_operations or LocalBashOperations(resolved_shell_sessions)
        return cls(
            agent=agent,
            provider=resolved_provider,
            tool_bus=resolved_bus,
            activation=activation,
            context_assembler=context_assembler or ContextAssembler(),
            lifecycle_hooks=lifecycle_hooks or NoopLifecycleHooks(),
            session_service=session_service,
            file_access_policy=resolved_policy,
            workspace_files=workspace_files,
            shell_session_manager=resolved_shell_sessions,
            skill_store=skill_store,
            unit_window=unit_window,
            strategy=strategy or ReActStrategy(),
            environ=Environ(),
            recall_sources=list(recall_sources or []),
            bash_operations=resolved_bash,
        )

    # --- ActivationControl 协议：供 tool_set_active 收窄/恢复激活集 ---

    def allowed_names(self) -> frozenset[str]:
        return self.activation.allowed

    def disable_tools(self, names: Iterable[str]) -> None:
        """agent 自我收窄激活集，下一 turn 生效（本 turn 快照已取）。"""
        self.activation = self.activation.with_agent_disabled(names)

    def enable_tools(self, names: Iterable[str]) -> None:
        self.activation = self.activation.with_agent_enabled(names)

    def apply_environ_model(self, app_config: AppConfig) -> None:
        """按 Environ 叠层重新 resolve model，更新 agent.model_config 与 provider。"""
        base_selection = ModelSelection(
            provider=self.agent.model_config.provider,
            model=self.agent.model_config.model,
        )
        model_config = app_config.resolve_model_config(
            base_selection,
            environ=self.environ,
        )
        self.agent.model_config = model_config
        self.provider = create_llm_provider(model_config)

    @classmethod
    def reload(
        cls,
        *,
        boot: "Boot",
        old_run: Run,
        agent_id: str,
        session_service: SessionService | None = None,
    ) -> tuple[Agent, Run]:
        """磁盘资源热重载：重建 Run，保留 Environ 模型选择。"""
        agent, new_run = boot.build_run(
            agent_id=agent_id,
            session_service=session_service,
        )
        new_run.environ = old_run.environ
        # bus 是进程级的：reload 不该丢掉非内置来源的工具（T2 的 MCP 子进程等）
        new_run.tool_bus = old_run.tool_bus
        new_run.activation = ToolActivation(allowed=frozenset(agent.tool_ids))
        new_run.apply_environ_model(boot.app_config)
        return agent, new_run

    async def turn(
        self,
        *,
        session: Session,
        user_text: str,
        bus: "EventBus | None" = None,
        observer: Observer | None = None,
        host_calls: "HostCallClient | None" = None,
    ) -> AssistantMessage:
        turn_id = str(uuid4())
        identity = ObservationIdentity(session_id=session.session_id, turn_id=turn_id)
        with observation_scope(observer):
            timer = SpanTimer(
                "pickel.turn",
                identity,
                attributes={
                    "agent_id": self.agent.agent_id,
                    "provider": self.agent.model_config.provider,
                    "model": self.agent.model_config.model,
                },
            )
            try:
                with span_scope(timer.span_id):
                    reply, outcome = await self._execute_turn(
                        session=session,
                        user_text=user_text,
                        bus=bus,
                        turn_id=turn_id,
                        host_calls=host_calls,
                    )
            except asyncio.CancelledError:
                timer.finish(status="cancelled", attributes={"outcome": "cancelled"})
                raise
            except Exception as exc:
                timer.finish(
                    status="error",
                    attributes={"outcome": "failed"},
                    error=ErrorInfo.from_exception(exc, kind="runtime"),
                )
                raise
            timer.finish(
                status="denied" if outcome == "blocked" else "ok",
                attributes={"outcome": outcome},
            )
            return reply

    async def _execute_turn(
        self,
        *,
        session: Session,
        user_text: str,
        bus: "EventBus | None",
        turn_id: str,
        host_calls: "HostCallClient | None",
    ) -> tuple[AssistantMessage, str]:
        """turn 边界：UserPromptSubmit hook → 写 user → strategy.execute。"""
        if session.agent_id != self.agent.agent_id:
            raise ValueError(
                f"Session '{session.session_id}' belongs to agent '{session.agent_id}', "
                f"not '{self.agent.agent_id}'"
            )

        def envelope() -> EventEnvelope:
            return EventEnvelope(session_id=session.session_id, turn_id=turn_id)

        async def emit(event) -> None:
            if bus is not None:
                await bus.emit(event)

        await emit(TurnStarted(envelope=envelope(), user_text=user_text))
        started = time.perf_counter()
        end_reason = "failed"
        try:
            decision = await self.lifecycle_hooks.user_prompt_submit(
                UserPromptSubmitEvent(
                    session_id=session.session_id,
                    turn_id=turn_id,
                    prompt=user_text,
                )
            )
            if decision.action == "block":
                end_reason = "blocked"
                blocked = AssistantMessage(
                    content=[TextContent(text=decision.reason or "请求被 Hook 阻止")]
                )
                await emit(
                    TurnCompleted(
                        envelope=envelope(),
                        usage=None,
                        elapsed_ms=round((time.perf_counter() - started) * 1000),
                        outcome="blocked",
                    )
                )
                return blocked, "blocked"

            user_entry = session.append_user(
                UserMessage(content=[TextContent(text=user_text)])
            )
            if self.session_service is not None:
                flush_timer = SpanTimer(
                    "pickel.session.append",
                    ObservationIdentity(session_id=session.session_id, turn_id=turn_id),
                    attributes={"entry_type": user_entry.entry_type, "entry_count": 1},
                )
                try:
                    self.session_service.flush_new_entries(
                        session=session,
                        entries=[user_entry],
                    )
                except Exception as exc:
                    flush_timer.finish(
                        status="error",
                        error=ErrorInfo.from_exception(exc, kind="storage"),
                    )
                    raise
                flush_timer.finish()

            reply = await self.strategy.execute(
                run=self,
                session=session,
                bus=bus,
                turn_id=turn_id,
                host_calls=host_calls,
                initial_hook_feedback=(
                    [
                        HookFeedback(
                            source_event="UserPromptSubmit",
                            text=decision.feedback_text,
                        )
                    ]
                    if decision.feedback_text
                    else None
                ),
            )
            end_reason = "completed"
            await emit(
                TurnCompleted(
                    envelope=envelope(),
                    usage=last_turn_usage(session),
                    elapsed_ms=round((time.perf_counter() - started) * 1000),
                )
            )
            return reply, "completed"
        except asyncio.CancelledError:
            end_reason = "cancelled"
            raise
        except Exception as exc:
            await emit(
                TurnFailed(
                    envelope=envelope(),
                    error_type=type(exc).__name__,
                    message=str(exc),
                    traceback_text=traceback.format_exc(),
                )
            )
            raise
        finally:
            await self.lifecycle_hooks.turn_end(
                TurnEndEvent(
                    session_id=session.session_id,
                    turn_id=turn_id,
                    reason=end_reason,
                )
            )

    def get_tool_execution_context(
        self,
        session_id: str,
        *,
        turn_id: str = "",
        step_index: int | None = None,
        tool_call_id: str = "",
        host_calls: "HostCallClient | None" = None,
    ) -> ToolExecutionContext:
        return ToolExecutionContext(
            agent_id=self.agent.agent_id,
            session_id=session_id,
            workspace_path=self.agent.workspace,
            services=ToolServices(
                workspace_files=self.workspace_files,
                bash=self.bash_operations,
                activation_control=self,
                skill_store=self.skill_store,
                host_calls=host_calls,
            ),
            turn_id=turn_id,
            step_index=step_index,
            tool_call_id=tool_call_id,
        )

    @staticmethod
    def _policy_for_mode(mode: str) -> FileAccessPolicy:
        if mode == FileAccessMode.FULL.value or mode == "full":
            return FullAccessPathPolicy()
        return WorkspacePathAccessPolicy()
