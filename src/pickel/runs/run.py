"""Run：运行资源袋 + turn 边界。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from pickel.agents.agent import Agent
from pickel.config.environ import Environ
from pickel.context.assembler import ContextAssembler
from pickel.context.hook_feedback import HookFeedback
from pickel.conversations.agent_message import AssistantMessage, UserMessage
from pickel.conversations.content_blocks import TextContent
from pickel.conversations.service import SessionService
from pickel.conversations.session import Session
from pickel.hooks.events import UserPromptSubmitEvent
from pickel.hooks.lifecycle import LifecycleHooks, NoopLifecycleHooks
from pickel.providers import create_llm_provider
from pickel.providers.base import Provider
from pickel.runs.events import RuntimeEventHandler
from pickel.shared.file_access import FileAccessMode
from pickel.shared.model_config import ModelSelection
from pickel.tools.base import BaseTool, ToolExecutionContext
from pickel.tools.catalog import builtin_tools
from pickel.tools.file_service import WorkspaceFileService
from pickel.tools.policy import (
    FileAccessPolicy,
    FullAccessPathPolicy,
    WorkspacePathAccessPolicy,
)
from pickel.tools.registry import ToolRegistry
from pickel.tools.shell import ShellSessionManager

if TYPE_CHECKING:
    from pickel.config.app_config import AppConfig
    from pickel.runs.strategy.base import ExecutionStrategy


@dataclass
class Run:
    """单次/会话级运行资源；open 构造，turn 执行用户一轮输入。"""

    agent: Agent
    provider: Provider
    tools: list[BaseTool]
    context_assembler: ContextAssembler
    lifecycle_hooks: LifecycleHooks
    session_service: SessionService | None
    file_access_policy: FileAccessPolicy | None
    workspace_files: WorkspaceFileService | None
    shell_session_manager: ShellSessionManager
    unit_window: int
    strategy: ExecutionStrategy
    environ: Environ = field(default_factory=Environ)

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
        provider: Provider | None = None,
        tools: list[BaseTool] | None = None,
        tool_registry: ToolRegistry | None = None,
    ) -> Run:
        """解析 provider/tools/workspace，组装 Run。"""
        from pickel.runs.strategy.react import ReActStrategy

        resolved_provider = provider or create_llm_provider(agent.model_config)
        if tools is None:
            registry = tool_registry or ToolRegistry(tools=builtin_tools())
            resolved_tools = registry.resolve_many(agent.tool_ids)
        else:
            resolved_tools = list(tools)

        resolved_policy = file_access_policy or cls._policy_for_mode(agent.file_access_mode)
        workspace_files = WorkspaceFileService(
            workspace_root=agent.workspace,
            access_policy=resolved_policy,
        )
        return cls(
            agent=agent,
            provider=resolved_provider,
            tools=resolved_tools,
            context_assembler=context_assembler or ContextAssembler(),
            lifecycle_hooks=lifecycle_hooks or NoopLifecycleHooks(),
            session_service=session_service,
            file_access_policy=resolved_policy,
            workspace_files=workspace_files,
            shell_session_manager=shell_session_manager or ShellSessionManager(),
            unit_window=unit_window,
            strategy=strategy or ReActStrategy(),
            environ=Environ(),
        )

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

    async def turn(
        self,
        *,
        session: Session,
        user_text: str,
        event_handler: RuntimeEventHandler | None = None,
    ) -> AssistantMessage:
        """turn 边界：UserPromptSubmit hook → 写 user → strategy.execute。"""
        if session.agent_id != self.agent.agent_id:
            raise ValueError(
                f"Session '{session.session_id}' belongs to agent '{session.agent_id}', "
                f"not '{self.agent.agent_id}'"
            )

        decision = await self.lifecycle_hooks.user_prompt_submit(
            UserPromptSubmitEvent(
                session_id=session.session_id,
                prompt=user_text,
            )
        )
        if decision.action == "block":
            return AssistantMessage(
                content=[TextContent(text=decision.reason or "请求被 Hook 阻止")]
            )

        user_entry = session.append_user(
            UserMessage(content=[TextContent(text=user_text)])
        )
        if self.session_service is not None:
            self.session_service.flush_new_entries(
                session=session,
                entries=[user_entry],
            )

        return await self.strategy.execute(
            run=self,
            session=session,
            event_handler=event_handler,
            initial_hook_feedback=(
                [HookFeedback(source_event="UserPromptSubmit", text=decision.feedback_text)]
                if decision.feedback_text
                else None
            ),
        )

    def get_tool_execution_context(self, session_id: str) -> ToolExecutionContext:
        return ToolExecutionContext(
            agent_id=self.agent.agent_id,
            session_id=session_id,
            workspace_path=self.agent.workspace,
            workspace_files=self.workspace_files,
            shell_session_manager=self.shell_session_manager,
        )

    @staticmethod
    def _policy_for_mode(mode: str) -> FileAccessPolicy:
        if mode == FileAccessMode.FULL.value or mode == "full":
            return FullAccessPathPolicy()
        return WorkspacePathAccessPolicy()
