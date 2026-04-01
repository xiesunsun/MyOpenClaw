from pathlib import Path
import unittest

from myopenclaw.tools.base import (
    ToolExecutionContext,
    ToolExecutionResult,
    tool,
)


class ToolDecoratorTests(unittest.IsolatedAsyncioTestCase):
    async def test_decorator_expands_named_arguments_and_injects_context(self) -> None:
        @tool(
            name="greet",
            description="Greet someone",
            input_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                },
                "required": ["name"],
            },
        )
        async def greet(name: str, context: ToolExecutionContext) -> str:
            return f"{name}@{context.workspace_path}"

        result = await greet.execute(
            {"name": "pickle"},
            ToolExecutionContext(
                agent_id="Pickle",
                session_id="session-1",
                workspace_path=Path("/tmp/pickle"),
                path_policy=None,
                shell_session_manager=None,
            ),
        )

        self.assertEqual("greet", greet.spec.name)
        self.assertEqual("pickle@/tmp/pickle", result.content)
        self.assertFalse(result.is_error)

    async def test_decorator_supports_raw_arguments_parameter_and_structured_result(self) -> None:
        @tool(
            name="inspect",
            description="Inspect arguments",
            input_schema={
                "type": "object",
                "properties": {
                    "value": {"type": "string"},
                },
                "required": ["value"],
            },
        )
        async def inspect_tool(
            arguments: dict[str, object],
            context: ToolExecutionContext,
        ) -> ToolExecutionResult:
            return ToolExecutionResult(
                content=f"{arguments['value']}:{context.agent_id}",
                metadata={"seen": True},
            )

        result = await inspect_tool.execute(
            {"value": "ping"},
            ToolExecutionContext(
                agent_id="Pickle",
                session_id="session-1",
                workspace_path=Path("/tmp/pickle"),
                path_policy=None,
                shell_session_manager=None,
            ),
        )

        self.assertEqual("ping:Pickle", result.content)
        self.assertEqual({"seen": True}, result.metadata)

    def test_tool_execution_context_exposes_runtime_dependencies(self) -> None:
        context = ToolExecutionContext(
            agent_id="Pickle",
            session_id="session-1",
            workspace_path=Path("/tmp/pickle"),
            path_policy="policy",
            shell_session_manager="shell-manager",
        )

        self.assertEqual("policy", context.path_policy)
        self.assertEqual("shell-manager", context.shell_session_manager)


if __name__ == "__main__":
    unittest.main()
