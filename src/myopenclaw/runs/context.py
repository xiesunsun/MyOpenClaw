"""兼容层：AgentRuntimeContext.create → RunDependencies。"""

from __future__ import annotations

from dataclasses import dataclass, field

from myopenclaw.agents.agent import Agent
from myopenclaw.context.assembler import ContextAssembler
from myopenclaw.providers import BaseLLMProvider, create_llm_provider
from myopenclaw.runs.dependencies import RunDependencies
from myopenclaw.shared.file_access import FileAccessMode
from myopenclaw.shared.model_config import ModelConfig
from myopenclaw.tools.base import BaseTool
from myopenclaw.tools.catalog import builtin_tools
from myopenclaw.tools.file_service import WorkspaceFileService
from myopenclaw.tools.policy import (
    FileAccessPolicy,
    FullAccessPathPolicy,
    WorkspacePathAccessPolicy,
)
from myopenclaw.tools.registry import ToolRegistry
from myopenclaw.tools.shell import ShellSessionManager
from myopenclaw.hooks.lifecycle import NoopLifecycleHooks


class DefaultProviderResolver:
    def resolve(self, model_config: ModelConfig) -> BaseLLMProvider:
        return create_llm_provider(model_config)


class DefaultToolResolver:
    def __init__(self, registry: ToolRegistry | None = None) -> None:
        self.registry = registry or ToolRegistry(tools=builtin_tools())

    def resolve(self, tool_ids: list[str]) -> list[BaseTool]:
        return self.registry.resolve_many(tool_ids)


@dataclass
class AgentRuntimeContext:
    """历史名：语义上等同 RunDependencies 的构造入口。"""

    agent: Agent
    provider: BaseLLMProvider
    tools: list[BaseTool]
    file_access_policy: FileAccessPolicy | None = None
    workspace_files: WorkspaceFileService | None = None
    shell_session_manager: ShellSessionManager = field(default_factory=ShellSessionManager)
    context_assembler: ContextAssembler = field(default_factory=ContextAssembler)
    unit_window: int = 5

    def __post_init__(self) -> None:
        if self.file_access_policy is None:
            self.file_access_policy = self._policy_for_mode(self.agent.file_access_mode)
        if self.workspace_files is None:
            self.workspace_files = WorkspaceFileService(
                workspace_root=self.agent.workspace,
                access_policy=self.file_access_policy,
            )

    @classmethod
    def create(
        cls,
        agent: Agent,
        provider_resolver: DefaultProviderResolver | None = None,
        tool_resolver: DefaultToolResolver | None = None,
        file_access_policy: FileAccessPolicy | None = None,
        shell_session_manager: ShellSessionManager | None = None,
        context_assembler: ContextAssembler | None = None,
        unit_window: int = 5,
        **_ignored,
    ) -> "AgentRuntimeContext":
        provider_resolver = provider_resolver or DefaultProviderResolver()
        tool_resolver = tool_resolver or DefaultToolResolver()
        provider = provider_resolver.resolve(agent.model_config)
        tools = tool_resolver.resolve(agent.tool_ids)
        resolved_policy = file_access_policy or cls._policy_for_mode(agent.file_access_mode)
        workspace_files = WorkspaceFileService(
            workspace_root=agent.workspace,
            access_policy=resolved_policy,
        )
        kwargs = {
            "agent": agent,
            "provider": provider,
            "tools": tools,
            "file_access_policy": resolved_policy,
            "workspace_files": workspace_files,
            "context_assembler": context_assembler or ContextAssembler(),
            "unit_window": unit_window,
        }
        if shell_session_manager is not None:
            kwargs["shell_session_manager"] = shell_session_manager
        return cls(**kwargs)

    def to_run_dependencies(self, session_service=None) -> RunDependencies:
        return RunDependencies(
            agent=self.agent,
            provider=self.provider,
            tools=self.tools,
            context_assembler=self.context_assembler,
            lifecycle_hooks=NoopLifecycleHooks(),
            session_service=session_service,
            file_access_policy=self.file_access_policy,
            workspace_files=self.workspace_files,
            shell_session_manager=self.shell_session_manager,
            unit_window=self.unit_window,
        )

    def get_tool_execution_context(self, session_id: str):
        return self.to_run_dependencies().get_tool_execution_context(session_id)

    @staticmethod
    def _policy_for_mode(mode: str) -> FileAccessPolicy:
        if mode == FileAccessMode.FULL.value or mode == "full":
            return FullAccessPathPolicy()
        return WorkspacePathAccessPolicy()
