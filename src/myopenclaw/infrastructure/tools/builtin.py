from myopenclaw.infrastructure.tools.base import ToolExecutionContext, tool


@tool(
    name="echo",
    description="Return the provided text back to the agent.",
    input_schema={
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": "The text to echo back.",
            }
        },
        "required": ["text"],
    },
)
async def echo(text: str, context: ToolExecutionContext) -> str:
    return text
