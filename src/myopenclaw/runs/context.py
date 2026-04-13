from dataclasses import dataclass, field

from myopenclaw.agents.agent import Agent
from myopenclaw.context import ConversationContextService
from myopenclaw.providers import BaseLLMProvider, create_llm_provider
from myopenclaw.shared.file_access import FileAccessMode
from myopenclaw.shared.model_config import ModelConfig
from myopenclaw.tools.base import BaseTool, ToolExecutionContext
from myopenclaw.tools.catalog import builtin_tools
from myopenclaw.tools.file_service import WorkspaceFileService
from myopenclaw.tools.policy import (
    FileAccessPolicy,
    FullAccessPathPolicy,
    WorkspacePathAccessPolicy,
)
from myopenclaw.tools.registry import ToolRegistry
from myopenclaw.tools.shell import ShellSessionManager


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
    agent: Agent
    provider: BaseLLMProvider
    tools: list[BaseTool]
    file_access_policy: FileAccessPolicy | None = None
    workspace_files: WorkspaceFileService | None = None
    shell_session_manager: ShellSessionManager = field(default_factory=ShellSessionManager)
    conversation_context_service: ConversationContextService = field(
        default_factory=ConversationContextService
    )

    def __post_init__(self) -> None:
        if self.file_access_policy is None:
            self.file_access_policy = self._resolve_file_access_policy()
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
        conversation_context_service: ConversationContextService | None = None,
    ) -> "AgentRuntimeContext":
        provider_resolver = provider_resolver or DefaultProviderResolver()
        tool_resolver = tool_resolver or DefaultToolResolver()

        provider = provider_resolver.resolve(agent.model_config)
        tools = tool_resolver.resolve(agent.tool_ids)
        resolved_file_access_policy = file_access_policy or cls._policy_for_mode(
            agent.file_access_mode
        )
        workspace_files = WorkspaceFileService(
            workspace_root=agent.workspace,
            access_policy=resolved_file_access_policy,
        )

        kwargs = {
            "agent": agent,
            "provider": provider,
            "tools": tools,
            "file_access_policy": resolved_file_access_policy,
            "workspace_files": workspace_files,
        }
        if shell_session_manager is not None:
            kwargs["shell_session_manager"] = shell_session_manager
        if conversation_context_service is not None:
            kwargs["conversation_context_service"] = conversation_context_service

        return cls(**kwargs)

    def get_tool_execution_context(self, session_id: str) -> ToolExecutionContext:
        return ToolExecutionContext(
            agent_id=self.agent.agent_id,
            session_id=session_id,
            workspace_path=self.agent.workspace,
            workspace_files=self.workspace_files,
            shell_session_manager=self.shell_session_manager,
        )

    def _resolve_file_access_policy(self) -> FileAccessPolicy:
        return self._policy_for_mode(self.agent.file_access_mode)

    @staticmethod
    def _policy_for_mode(mode: str) -> FileAccessPolicy:
        if mode == FileAccessMode.FULL.value:
            return FullAccessPathPolicy()
        return WorkspacePathAccessPolicy()
