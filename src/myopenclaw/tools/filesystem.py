from __future__ import annotations

from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

from myopenclaw.tools.base import BaseTool, ToolExecutionContext, ToolExecutionResult, ToolSpec


def _require_path_policy(context: ToolExecutionContext):
    if context.path_policy is None:
        raise RuntimeError("A path policy is required for filesystem tools")
    return context.path_policy


def _format_lines(lines: list[str], start_line: int) -> str:
    return "\n".join(f"{index}: {line}" for index, line in enumerate(lines, start=start_line))


def _truncate_text(text: str, max_chars: int | None) -> tuple[str, bool]:
    if max_chars is None or len(text) <= max_chars:
        return text, False
    return text[:max_chars], True


def _write_text_atomic(path: Path, content: str) -> None:
    with NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        handle.write(content)
        temp_path = Path(handle.name)
    temp_path.replace(path)


class ReadTool(BaseTool):
    spec = ToolSpec(
        name="read",
        description="Read a text file from the workspace. Returns content with line numbers.",
        input_schema={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative or absolute path to the file to read.",
                },
                "start_line": {
                    "type": "integer",
                    "description": "The line number to start reading from (1-indexed). Defaults to 1.",
                },
                "end_line": {
                    "type": "integer",
                    "description": "The line number to end reading at. Defaults to the end of the file.",
                },
                "max_chars": {
                    "type": "integer",
                    "description": "Maximum number of characters to read. Used for truncation.",
                },
            },
            "required": ["path"],
        },
    )

    async def execute(
        self,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
    ) -> ToolExecutionResult:
        policy = _require_path_policy(context)
        try:
            resolved_path = policy.resolve(str(arguments["path"]), context.workspace_path)
            policy.assert_readable(resolved_path)
            lines = resolved_path.read_text(encoding="utf-8").splitlines()
            start_line = max(int(arguments.get("start_line", 1)), 1)
            end_line = int(arguments.get("end_line", len(lines)))
            selected_lines = lines[start_line - 1:end_line]
            content = _format_lines(selected_lines, start_line)
            content, truncated = _truncate_text(content, arguments.get("max_chars"))
            return ToolExecutionResult(
                content=content,
                metadata={
                    "path": str(resolved_path),
                    "start_line": start_line,
                    "end_line": start_line + max(len(selected_lines) - 1, 0),
                    "truncated": truncated,
                },
            )
        except Exception as exc:
            return ToolExecutionResult(content=str(exc), is_error=True)


class WriteTool(BaseTool):
    spec = ToolSpec(
        name="write",
        description="Write, edit, or append to a text file in the workspace.",
        input_schema={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative or absolute path to the file to modify.",
                },
                "action": {
                    "type": "string",
                    "enum": ["create", "overwrite", "append", "replace", "insert"],
                    "description": (
                        "The write operation to perform: "
                        "'create' (new file only), "
                        "'overwrite' (replace whole file), "
                        "'append' (add to end), "
                        "'replace' (exact string replacement), "
                        "'insert' (insert at line)."
                    ),
                },
                "content": {
                    "type": "string",
                    "description": "Content to write for create/overwrite/append/insert.",
                },
                "old_text": {
                    "type": "string",
                    "description": "Exact text to find for the 'replace' action.",
                },
                "new_text": {
                    "type": "string",
                    "description": "The replacement text for the 'replace' action.",
                },
                "insert_line": {
                    "type": "integer",
                    "description": "The line number (1-indexed) to insert 'content' before.",
                },
            },
            "required": ["path", "action"],
        },
    )

    async def execute(
        self,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
    ) -> ToolExecutionResult:
        policy = _require_path_policy(context)
        try:
            resolved_path = policy.resolve(str(arguments["path"]), context.workspace_path)
            policy.assert_writable(resolved_path)
            action = str(arguments["action"])
            current_content = resolved_path.read_text(encoding="utf-8") if resolved_path.exists() else ""

            if action == "create":
                if resolved_path.exists():
                    raise FileExistsError(f"File already exists: {resolved_path}")
                next_content = str(arguments.get("content", ""))
            elif action == "overwrite":
                next_content = str(arguments.get("content", ""))
            elif action == "append":
                next_content = current_content + str(arguments.get("content", ""))
            elif action == "replace":
                old_text = str(arguments.get("old_text", ""))
                new_text = str(arguments.get("new_text", ""))
                if old_text not in current_content:
                    raise ValueError(f"Text not found in file: {old_text!r}")
                next_content = current_content.replace(old_text, new_text, 1)
            elif action == "insert":
                insert_line = max(int(arguments.get("insert_line", 1)), 1)
                lines = current_content.splitlines()
                insertion = str(arguments.get("content", ""))
                lines.insert(insert_line - 1, insertion)
                next_content = "\n".join(lines)
                if current_content.endswith("\n"):
                    next_content += "\n"
            else:
                raise ValueError(f"Unsupported write action: {action}")

            _write_text_atomic(resolved_path, next_content)
            return ToolExecutionResult(
                content=f"{action} -> {resolved_path}",
                metadata={
                    "path": str(resolved_path),
                    "action": action,
                    "bytes_written": len(next_content.encode("utf-8")),
                },
            )
        except Exception as exc:
            return ToolExecutionResult(content=str(exc), is_error=True)
