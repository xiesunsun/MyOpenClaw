from __future__ import annotations

from typing import Any, NoReturn

from pickel.tools.base import (
    BaseTool,
    ToolExecutionContext,
    ToolExecutionError,
    ToolSpec,
)
from pickel.tools.file_errors import FileToolError
from pickel.tools.file_formatter import FileToolFormatter
from pickel.tools.file_service import WorkspaceFileService

DEFAULT_READ_LINES = 2_000
DEFAULT_READ_CHARS = 50_000


def _require_workspace_files(context: ToolExecutionContext) -> WorkspaceFileService:
    if context.services.workspace_files is None:
        raise RuntimeError("A workspace file service is required for file tools")
    return context.services.workspace_files


def _truncate_read(
    text: str, *, source_path: str, next_offset: int
) -> tuple[str, bool]:
    if len(text) <= DEFAULT_READ_CHARS:
        return text, False
    marker = (
        f"\n[Output truncated; truncated=true; preview capped at {DEFAULT_READ_CHARS} characters. "
        f"Full source remains at path={source_path}; use read(path={source_path!r}, "
        f"offset={next_offset}) to continue.]"
    )
    return text[: DEFAULT_READ_CHARS - len(marker)] + marker, True


class BaseFileTool(BaseTool):
    def __init__(self, formatter: FileToolFormatter) -> None:
        self.formatter = formatter

    def _error_result(self, exc: Exception) -> NoReturn:
        raise ToolExecutionError(str(exc)) from exc


class LsTool(BaseFileTool):
    spec = ToolSpec(
        name="ls",
        description=(
            "List the immediate contents of a workspace directory. Returns one "
            "workspace-relative path per line, sorted alphabetically; directories end "
            "with '/'. Includes hidden entries and does not recurse."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Workspace directory to list.",
                    "default": ".",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Maximum directory entries to return.",
                    "default": 500,
                },
            },
            "additionalProperties": False,
        },
        output_schema={"type": "string"},
    )

    async def execute(
        self, arguments: dict[str, Any], context: ToolExecutionContext
    ) -> str:
        try:
            result = _require_workspace_files(context).list_directory(
                path=str(arguments.get("path", ".")),
                recursive=False,
                include_hidden=True,
                max_entries=int(arguments.get("limit", 500)),
            )
            return self.formatter.format_directory_listing(result)
        except (FileToolError, RuntimeError, ValueError) as exc:
            return self._error_result(exc)


class GlobTool(BaseFileTool):
    spec = ToolSpec(
        name="glob",
        description=(
            "Find workspace files whose paths relative to the search directory match a "
            "glob pattern. Returns one workspace-relative file path per line. Includes "
            "hidden files and respects Git ignore rules."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": (
                        "Glob matched against paths relative to the search directory, "
                        "for example '**/*.py'."
                    ),
                },
                "path": {
                    "type": "string",
                    "description": "Workspace directory to search.",
                    "default": ".",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Maximum file paths to return.",
                    "default": 1_000,
                },
            },
            "required": ["pattern"],
            "additionalProperties": False,
        },
        output_schema={"type": "string"},
    )

    async def execute(
        self, arguments: dict[str, Any], context: ToolExecutionContext
    ) -> str:
        try:
            result = _require_workspace_files(context).glob_search(
                pattern=str(arguments["pattern"]),
                base_path=str(arguments.get("path", ".")),
                max_results=int(arguments.get("limit", 1_000)),
            )
            return self.formatter.format_glob_search(result)
        except (FileToolError, RuntimeError, ValueError) as exc:
            return self._error_result(exc)


class GrepTool(BaseFileTool):
    spec = ToolSpec(
        name="grep",
        description=(
            "Search workspace text files with a regular expression. Returns one match "
            "per line as path:line:text. Includes hidden files, skips binary files, "
            "and respects Git ignore rules."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Regular expression to search for.",
                },
                "path": {
                    "type": "string",
                    "description": "Workspace directory to search.",
                    "default": ".",
                },
                "glob": {
                    "type": "string",
                    "description": (
                        "Optional glob matched against paths relative to the search "
                        "directory, for example '*.py' or '**/test_*.py'."
                    ),
                },
                "ignore_case": {
                    "type": "boolean",
                    "description": "Use case-insensitive matching.",
                    "default": False,
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Maximum matching lines to return.",
                    "default": 100,
                },
            },
            "required": ["pattern"],
            "additionalProperties": False,
        },
        output_schema={"type": "string"},
    )

    async def execute(
        self, arguments: dict[str, Any], context: ToolExecutionContext
    ) -> str:
        try:
            result = _require_workspace_files(context).grep_search(
                pattern=str(arguments["pattern"]),
                base_path=str(arguments.get("path", ".")),
                glob_pattern=(
                    str(arguments["glob"])
                    if arguments.get("glob") is not None
                    else None
                ),
                case_sensitive=not bool(arguments.get("ignore_case", False)),
                max_results=int(arguments.get("limit", 100)),
            )
            return self.formatter.format_grep_search(result)
        except (FileToolError, RuntimeError, ValueError) as exc:
            return self._error_result(exc)


class ReadTool(BaseFileTool):
    spec = ToolSpec(
        name="read",
        description=(
            "Read a UTF-8 text file with 1-indexed line numbers. Reads at most limit "
            "lines beginning at offset; truncated results report the next offset. Use "
            "bash for binary files or unusually long lines. Model-visible output is "
            f"capped at {DEFAULT_READ_CHARS:,} characters."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File to read."},
                "offset": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "First line to read, 1-indexed.",
                    "default": 1,
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "description": (
                        "Maximum lines to read before returning a continuation offset."
                    ),
                    "default": DEFAULT_READ_LINES,
                },
            },
            "required": ["path"],
            "additionalProperties": False,
        },
        output_schema={"type": "string"},
    )

    async def execute(
        self, arguments: dict[str, Any], context: ToolExecutionContext
    ) -> str:
        try:
            offset = int(arguments.get("offset", 1))
            limit = int(arguments.get("limit", DEFAULT_READ_LINES))
            result = _require_workspace_files(context).read_file(
                path=str(arguments["path"]),
                start_line=offset,
                end_line=offset + limit - 1,
            )
            content, chars_truncated = _truncate_read(
                self.formatter.format_file_read(result),
                source_path=result.path,
                next_offset=result.end_line + 1,
            )
            return content
        except (FileToolError, RuntimeError, ValueError) as exc:
            return self._error_result(exc)


class EditTool(BaseFileTool):
    spec = ToolSpec(
        name="edit",
        description=(
            "Replace one exact, unique text span in a UTF-8 file. Fails without "
            "changing the file when the text is absent or occurs more than once. "
            "Returns a unified diff."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File to edit."},
                "old_text": {
                    "type": "string",
                    "description": "Exact text to replace. It must occur exactly once.",
                },
                "new_text": {"type": "string", "description": "Replacement text."},
            },
            "required": ["path", "old_text", "new_text"],
            "additionalProperties": False,
        },
        output_schema={"type": "string"},
    )

    async def execute(
        self, arguments: dict[str, Any], context: ToolExecutionContext
    ) -> str:
        try:
            result = _require_workspace_files(context).replace_exact(
                path=str(arguments["path"]),
                old_text=str(arguments["old_text"]),
                new_text=str(arguments["new_text"]),
            )
            return self.formatter.format_replace(result)
        except (FileToolError, RuntimeError, ValueError) as exc:
            return self._error_result(exc)


class WriteTool(BaseFileTool):
    spec = ToolSpec(
        name="write",
        description=(
            "Create or completely overwrite a UTF-8 text file. Creates missing parent "
            "directories. Use edit for localized changes."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File to write."},
                "content": {"type": "string", "description": "Complete file content."},
            },
            "required": ["path", "content"],
            "additionalProperties": False,
        },
        output_schema={"type": "string"},
    )

    async def execute(
        self, arguments: dict[str, Any], context: ToolExecutionContext
    ) -> str:
        try:
            result = _require_workspace_files(context).write_file(
                path=str(arguments["path"]),
                content=str(arguments["content"]),
            )
            return self.formatter.format_write_file(result)
        except (FileToolError, RuntimeError, ValueError) as exc:
            return self._error_result(exc)
