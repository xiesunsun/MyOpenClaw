from pathlib import Path
import unittest
from dataclasses import fields

import pytest

from pickel.context.model_context import ToolDefinition
from pickel.tools.base import (
    ToolExecutionContext,
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
            output_schema={"type": "string"},
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
        self.assertEqual({"type": "string"}, greet.spec.output_schema)
        self.assertEqual("pickle@/tmp/pickle", result)
        self.assertEqual("pickle@/tmp/pickle", greet.render(result)[0].text)

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
            output_schema={"type": "object"},
        )
        async def inspect_tool(
            arguments: dict[str, object],
            context: ToolExecutionContext,
        ) -> dict[str, object]:
            return {"value": f"{arguments['value']}:{context.agent_id}", "seen": True}

        result = await inspect_tool.execute(
            {"value": "ping"},
            ToolExecutionContext(
                agent_id="Pickle",
                identity=ExecutionIdentity(session_id="session-1"),
                workspace_path=Path("/tmp/pickle"),
            ),
        )

        self.assertEqual(
            {"value": "ping:Pickle", "seen": True},
            result,
        )
        self.assertEqual(
            '{"seen":true,"value":"ping:Pickle"}',
            inspect_tool.render(result)[0].text,
        )

    def test_tool_schemas_are_deeply_frozen(self) -> None:
        nested_input = {"type": "object", "properties": {"x": {"type": "string"}}}
        nested_output = {"type": "object", "properties": {"ok": {"type": "boolean"}}}
        decorated = tool(
            name="frozen",
            description="frozen",
            input_schema=nested_input,
            output_schema=nested_output,
        )(lambda arguments, context: {"ok": True})

        nested_input["properties"]["x"]["type"] = "integer"
        nested_output["properties"]["ok"]["type"] = "string"
        self.assertEqual(
            "string", decorated.spec.input_schema["properties"]["x"]["type"]
        )
        self.assertEqual(
            "boolean", decorated.spec.output_schema["properties"]["ok"]["type"]
        )
        with self.assertRaises(TypeError):
            decorated.spec.output_schema["properties"]["ok"] = {}  # type: ignore[index]

    def test_tool_definition_requires_output_schema(self) -> None:
        with pytest.raises(TypeError, match="JSON object"):
            ToolDefinition("missing", "missing", {"type": "object"}, None)


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
