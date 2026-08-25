from pathlib import Path
import unittest
from dataclasses import fields

from pickel.tools.base import (
    ToolExecutionContext,
    ToolExecutionResult,
    tool,
)
from pickel.shared.execution_identity import ExecutionIdentity


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
                identity=ExecutionIdentity(session_id="session-1"),
                workspace_path=Path("/tmp/pickle"),
            ),
        )

        self.assertEqual("greet", greet.spec.name)
        self.assertEqual("pickle@/tmp/pickle", result.content)
        self.assertFalse(result.is_error)

    async def test_decorator_supports_raw_arguments_parameter_and_structured_result(
        self,
    ) -> None:
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
                identity=ExecutionIdentity(session_id="session-1"),
                workspace_path=Path("/tmp/pickle"),
            ),
        )

        self.assertEqual("ping:Pickle", result.content)
        self.assertEqual({"seen": True}, result.metadata)


class ToolServicesTests(unittest.TestCase):
    def test_context_keeps_execution_identity_as_single_identity_field(self) -> None:
        context = ToolExecutionContext(
            agent_id="Pickle",
            identity=ExecutionIdentity(
                session_id="session-1",
                operation_id="operation-1",
                step_id="step-1",
                step_sequence=2,
                tool_call_id="tool-call-1",
            ),
            workspace_path=Path("/tmp/pickle"),
        )

        self.assertEqual(
            {"agent_id", "identity", "workspace_path", "services"},
            {item.name for item in fields(ToolExecutionContext)},
        )
        self.assertEqual("operation-1", context.identity.operation_id)
        self.assertEqual("tool-call-1", context.identity.tool_call_id)
        self.assertFalse(hasattr(context, "session_id"))
        self.assertFalse(hasattr(context, "operation_id"))
        self.assertFalse(hasattr(context, "step_id"))
        self.assertFalse(hasattr(context, "step_sequence"))
        self.assertFalse(hasattr(context, "tool_call_id"))

    def test_context_defaults_to_empty_services(self) -> None:
        from pickel.tools.services import ToolServices

        context = ToolExecutionContext(
            agent_id="Pickle",
            identity=ExecutionIdentity(session_id="session-1"),
            workspace_path=Path("/tmp/pickle"),
        )

        self.assertIsInstance(context.services, ToolServices)
        self.assertIsNone(context.services.workspace_files)
        self.assertIsNone(context.services.bash)

    def test_services_carries_injected_dependencies(self) -> None:
        from pickel.tools.services import ToolServices

        services = ToolServices(workspace_files="fake-files", bash="fake-bash")
        context = ToolExecutionContext(
            agent_id="Pickle",
            identity=ExecutionIdentity(session_id="session-1"),
            workspace_path=Path("/tmp/pickle"),
            services=services,
        )

        self.assertEqual("fake-files", context.services.workspace_files)
        self.assertEqual("fake-bash", context.services.bash)


if __name__ == "__main__":
    unittest.main()
