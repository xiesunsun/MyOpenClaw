"""RunDependencies：单次运行依赖容器（替代 AgentRuntimeContext 的语义）。"""

from __future__ import annotations

from dataclasses import dataclass, field

from myopenclaw.agents.agent import Agent
from myopenclaw.context.assembler import ContextAssembler
from myopenclaw.conversations.service import SessionService
from myopenclaw.hooks.lifecycle import LifecycleHooks, NoopLifecycleHooks
from myopenclaw.providers.base import BaseLLMProvider
from myopenclaw.tools.base import BaseTool, ToolExecutionContext
from myopenclaw.tools.file_service import WorkspaceFileService
from myopenclaw.tools.policy import FileAccessPolicy
from myopenclaw.tools.shell import ShellSessionManager


@dataclass
class RunDependencies:
    agent: Agent
    provider: BaseLLMProvider
    tools: list[BaseTool]
    context_assembler: ContextAssembler
    lifecycle_hooks: LifecycleHooks = field(default_factory=NoopLifecycleHooks)
    session_service: SessionService | None = None
    file_access_policy: FileAccessPolicy | None = None
    workspace_files: WorkspaceFileService | None = None
    shell_session_manager: ShellSessionManager = field(default_factory=ShellSessionManager)
    unit_window: int = 5

    def get_tool_execution_context(self, session_id: str) -> ToolExecutionContext:
        return ToolExecutionContext(
            agent_id=self.agent.agent_id,
            session_id=session_id,
            workspace_path=self.agent.workspace,
            workspace_files=self.workspace_files,
            shell_session_manager=self.shell_session_manager,
        )
