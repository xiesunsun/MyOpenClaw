from pickel.tools.base import BaseTool, ToolExecutionResult, ToolSpec
from pickel.shared.frozen_json import freeze_json_object
from pickel.tools.validation import (
    validate_json_schema,
    validate_tool_arguments,
    validate_tool_result,
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


def test_validate_tool_result_uses_structured_content():
    assert (
        validate_tool_result(
            _Tool(), ToolExecutionResult(content="ok", structured_content={"name": "x"})
        )
        is None
    )
    assert (
        validate_tool_result(
            _Tool(), ToolExecutionResult(content="ok", structured_content={"name": 1})
        )
        is not None
    )


def test_validate_tool_result_rejects_non_json_structured_content():
    result = ToolExecutionResult(content="ok", structured_content={"value": object()})

    error = validate_tool_result(_Tool(), result)

    assert error is not None
    assert "不是 JSON 数据" in error


def test_validate_tool_result_requires_structured_content_when_schema_declared():
    error = validate_tool_result(_Tool(), ToolExecutionResult(content="ok"))

    assert error is not None
    assert "None is not of type 'object'" in error


def test_invalid_tool_result_drops_unserializable_structure():
    from pickel.tools.validation import invalid_tool_result

    result = invalid_tool_result(
        ToolExecutionResult(content="ok", structured_content={"value": object()}),
        "$.structured_content 不是 JSON 数据",
    )

    assert result.is_error is True
    assert result.structured_content is None
    assert result.error is not None
    assert result.error.type == "INVALID_TOOL_OUTPUT"
