from pickel.tools.base import BaseTool, ToolExecutionResult, ToolSpec
from pickel.tools.validation import validate_tool_arguments, validate_tool_result


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
