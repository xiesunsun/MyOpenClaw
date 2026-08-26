from pickel.context.context_usage import estimate_context_usage
from pickel.context.model_context import ModelContext, SystemContent, ToolDefinition


def test_tool_schema_estimate_uses_json_projection_of_frozen_schema() -> None:
    context = ModelContext(
        system=SystemContent(),
        messages=(),
        tools=(
            ToolDefinition(
                name="lookup",
                description="查询",
                input_schema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                    },
                },
                output_schema={"type": "object"},
            ),
        ),
    )

    usage = estimate_context_usage(
        context,
        model_label="openai / gpt-5.6-luna",
        max_input_tokens=1000,
    )

    tools = next(category for category in usage.categories if category.key == "tools")
    assert tools.tokens > 0
    assert usage.total_tokens >= tools.tokens
