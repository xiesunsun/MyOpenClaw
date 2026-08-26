from pickel.tools.base import BaseTool, ToolSpec
from pickel.shared.frozen_json import freeze_json_object
from pickel.tools.validation import (
    validate_json_schema,
    validate_tool_arguments,
    validate_tool_output,
)


class _Tool(BaseTool):
    spec = ToolSpec(
        name="lookup",
        description="lookup",
        input_schema={
            "type": "object",
            "properties": {"id": {"type": "integer"}},
            "required": ["id"],
            "additionalProperties": False,
        },
        output_schema={
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
    )

    async def execute(self, arguments, context):
        raise NotImplementedError


def test_validate_tool_arguments_reports_json_path():
    error = validate_tool_arguments(_Tool(), {"id": "7"})

    assert error is not None
    assert "$.id" in error


def test_validate_frozen_tool_schema_without_loading_implementation():
    schema = freeze_json_object(_Tool.spec.input_schema)
    error = validate_json_schema({"id": "7"}, schema)

    assert error is not None
    assert "$.id" in error
    assert validate_json_schema({"id": 7}, schema) is None


def test_validate_tool_output_uses_declared_schema():
    assert validate_tool_output(_Tool(), {"name": "x"}) is None
    assert validate_tool_output(_Tool(), {"name": 1}) is not None


def test_validate_tool_output_rejects_non_json_values():
    error = validate_tool_output(_Tool(), {"value": object()})

    assert error is not None
    assert "不是 JSON 数据" in error


def test_validate_tool_output_requires_declared_schema_value():
    error = validate_tool_output(_Tool(), {})

    assert error is not None
    assert "'name' is a required property" in error


def test_validate_tool_output_accepts_json_scalar_when_schema_allows_it():
    assert (
        validate_tool_output(
            type("Schema", (), {"output_schema": {"type": "string"}})(), "ok"
        )
        is None
    )
