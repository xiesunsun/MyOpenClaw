from typing import TYPE_CHECKING

from myopenclaw.llm import BaseLLMProvider, create_llm_provider
from myopenclaw.tools.base import BaseTool, ToolSpec

if TYPE_CHECKING:
    from myopenclaw.agent.definition import AgentDefinition


class Agent:
    def __init__(
        self,
        definition: "AgentDefinition",
        provider: BaseLLMProvider | None = None,
        tools: list[BaseTool] | None = None,
    ) -> None:
        self.definition = definition
        self.provider = provider or create_llm_provider(definition.model_config)
        self.tools = list(tools or [])

    @property
    def system_instruction(self) -> str:
        return self.definition.behavior_instruction

    @property
    def workspace(self):
        return self.definition.workspace_path

    @property
    def tool_specs(self) -> list[ToolSpec]:
        return [tool.spec for tool in self.tools]

    def get_tool(self, name: str) -> BaseTool | None:
        for tool in self.tools:
            if tool.spec.name == name:
                return tool
        return None
