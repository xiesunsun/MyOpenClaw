from myopenclaw.tools.base import BaseTool


class ToolRegistry:
    def __init__(self, tools: list[BaseTool] | None = None) -> None:
        self._tools = {
            tool.spec.name: tool
            for tool in (tools or [])
        }

    def register(self, tool: BaseTool) -> None:
        self._tools[tool.spec.name] = tool

    def resolve(self, tool_id: str) -> BaseTool:
        try:
            return self._tools[tool_id]
        except KeyError as exc:
            raise KeyError(f"Unknown tool: {tool_id}") from exc

    def resolve_many(self, tool_ids: list[str]) -> list[BaseTool]:
        return [self.resolve(tool_id) for tool_id in tool_ids]
