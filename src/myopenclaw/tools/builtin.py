from pathlib import Path

from myopenclaw.tools.base import ToolExecutionContext, tool


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


@tool(
    name="read",
    description="Read the contents of a text file",
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Path to the file to read",
            },
        },
        "required": ["path"],
    },
)
async def read_file(path: str, context: ToolExecutionContext) -> str:
    try:
        return Path(path).read_text()
    except FileNotFoundError:
        return f"Error: File not found: {path}"
    except PermissionError:
        return f"Error: Permission denied reading: {path}"
    except IsADirectoryError:
        return f"Error: Path is a directory, not a file: {path}"
    except Exception as exc:
        return f"Error reading file: {exc}"
