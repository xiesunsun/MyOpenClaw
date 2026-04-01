from dataclasses import dataclass, field

from myopenclaw.agent.agent import Agent
from myopenclaw.llm import BaseLLMProvider, create_llm_provider
from myopenclaw.llm.config import ModelConfig
from myopenclaw.tools.base import BaseTool, ToolExecutionContext
from myopenclaw.tools.catalog import builtin_tools
from myopenclaw.tools.policy import PathAccessPolicy, WorkspacePathAccessPolicy
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
    path_policy: PathAccessPolicy = field(default_factory=WorkspacePathAccessPolicy)
    shell_session_manager: ShellSessionManager = field(default_factory=ShellSessionManager)

    @classmethod
    def create(
        cls,
        agent: Agent,
        provider_resolver: DefaultProviderResolver | None = None,
        tool_resolver: DefaultToolResolver | None = None,
        path_policy: PathAccessPolicy | None = None,
        shell_session_manager: ShellSessionManager | None = None,
    ) -> "AgentRuntimeContext":
        provider_resolver = provider_resolver or DefaultProviderResolver()
        tool_resolver = tool_resolver or DefaultToolResolver()

        provider = provider_resolver.resolve(agent.model_config)
        tools = tool_resolver.resolve(agent.tool_ids)

        kwargs = {
            "agent": agent,
            "provider": provider,
            "tools": tools,
        }
        if path_policy is not None:
            kwargs["path_policy"] = path_policy
        if shell_session_manager is not None:
            kwargs["shell_session_manager"] = shell_session_manager

        return cls(**kwargs)

    def get_tool_execution_context(self, session_id: str) -> ToolExecutionContext:
        return ToolExecutionContext(
            agent_id=self.agent.agent_id,
            session_id=session_id,
            workspace_path=self.agent.workspace,
            path_policy=self.path_policy,
            shell_session_manager=self.shell_session_manager,
        )
