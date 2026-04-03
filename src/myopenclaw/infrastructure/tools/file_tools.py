from __future__ import annotations

from typing import Any

from myopenclaw.infrastructure.tools.base import BaseTool, ToolExecutionContext, ToolExecutionResult, ToolSpec
from myopenclaw.infrastructure.tools.file_errors import FileToolError
from myopenclaw.infrastructure.tools.file_formatter import FileToolFormatter
from myopenclaw.infrastructure.tools.file_service import WorkspaceFileService


def _require_workspace_files(context: ToolExecutionContext) -> WorkspaceFileService:
    if context.workspace_files is None:
        raise RuntimeError("A workspace file service is required for file tools")
    return context.workspace_files


def _truncate_text(text: str, max_chars: int | None) -> tuple[str, bool]:
    if max_chars is None or len(text) <= max_chars:
        return text, False
    return text[:max_chars], True


class BaseFileTool(BaseTool):
    def __init__(self, formatter: FileToolFormatter) -> None:
        self.formatter = formatter

    def _error_result(self, exc: Exception) -> ToolExecutionResult:
        return ToolExecutionResult(content=str(exc), is_error=True)


class ListDirectoryTool(BaseFileTool):
    spec = ToolSpec(
        name="list_directory",
        description="List files and directories in the workspace.",
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Directory path relative to the workspace root."},
                "recursive": {"type": "boolean", "description": "Whether to recursively list descendants."},
                "include_hidden": {"type": "boolean", "description": "Whether to include hidden files and directories."},
                "max_entries": {"type": "integer", "description": "Maximum number of entries to return."},
            },
        },
    )

    async def execute(self, arguments: dict[str, Any], context: ToolExecutionContext) -> ToolExecutionResult:
        try:
            result = _require_workspace_files(context).list_directory(
                path=str(arguments.get("path", ".")),
                recursive=bool(arguments.get("recursive", False)),
                include_hidden=bool(arguments.get("include_hidden", False)),
                max_entries=int(arguments.get("max_entries", 200)),
            )
            return ToolExecutionResult(
                content=self.formatter.format_directory_listing(result),
                metadata={
                    "path": result.base_path,
                    "returned_count": len(result.entries),
                    "truncated": result.truncated,
                },
            )
        except (FileToolError, RuntimeError, ValueError) as exc:
            return self._error_result(exc)


class GlobSearchTool(BaseFileTool):
    spec = ToolSpec(
        name="glob_search",
        description="Find workspace paths matching a glob pattern.",
        input_schema={
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Glob pattern to match."},
                "base_path": {"type": "string", "description": "Directory path relative to the workspace root."},
                "max_results": {"type": "integer", "description": "Maximum number of matches to return."},
            },
            "required": ["pattern"],
        },
    )

    async def execute(self, arguments: dict[str, Any], context: ToolExecutionContext) -> ToolExecutionResult:
        try:
            result = _require_workspace_files(context).glob_search(
                pattern=str(arguments["pattern"]),
                base_path=str(arguments.get("base_path", ".")),
                max_results=int(arguments.get("max_results", 200)),
            )
            return ToolExecutionResult(
                content=self.formatter.format_glob_search(result),
                metadata={
                    "base_path": result.base_path,
                    "pattern": result.pattern,
                    "returned_count": len(result.matches),
                    "truncated": result.truncated,
                },
            )
        except (FileToolError, RuntimeError, ValueError) as exc:
            return self._error_result(exc)


class GrepSearchTool(BaseFileTool):
    spec = ToolSpec(
        name="grep_search",
        description="Search workspace files for content matches.",
        input_schema={
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Regular expression pattern to search for."},
                "base_path": {"type": "string", "description": "Directory path relative to the workspace root."},
                "glob_pattern": {"type": "string", "description": "Optional filename glob filter."},
                "case_sensitive": {"type": "boolean", "description": "Whether matching is case-sensitive."},
                "max_results": {"type": "integer", "description": "Maximum number of hits to return."},
            },
            "required": ["pattern"],
        },
    )

    async def execute(self, arguments: dict[str, Any], context: ToolExecutionContext) -> ToolExecutionResult:
        try:
            result = _require_workspace_files(context).grep_search(
                pattern=str(arguments["pattern"]),
                base_path=str(arguments.get("base_path", ".")),
                glob_pattern=(
                    str(arguments["glob_pattern"])
                    if arguments.get("glob_pattern") is not None
                    else None
                ),
                case_sensitive=bool(arguments.get("case_sensitive", False)),
                max_results=int(arguments.get("max_results", 100)),
            )
            return ToolExecutionResult(
                content=self.formatter.format_grep_search(result),
                metadata={
                    "pattern": result.pattern,
                    "returned_count": len(result.hits),
                    "truncated": result.truncated,
                },
            )
        except (FileToolError, RuntimeError, ValueError) as exc:
            return self._error_result(exc)


class ReadFileTool(BaseFileTool):
    spec = ToolSpec(
        name="read_file",
        description="Read a text file from the workspace with line numbers.",
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to the file to read."},
                "start_line": {"type": "integer", "description": "1-indexed first line to read."},
                "end_line": {"type": "integer", "description": "1-indexed last line to read."},
                "max_chars": {"type": "integer", "description": "Maximum number of characters to return."},
            },
            "required": ["path"],
        },
    )

    async def execute(self, arguments: dict[str, Any], context: ToolExecutionContext) -> ToolExecutionResult:
        try:
            result = _require_workspace_files(context).read_file(
                path=str(arguments["path"]),
                start_line=max(int(arguments.get("start_line", 1)), 1),
                end_line=(
                    int(arguments["end_line"])
                    if arguments.get("end_line") is not None
                    else None
                ),
            )
            content, truncated = _truncate_text(
                self.formatter.format_file_read(result),
                arguments.get("max_chars"),
            )
            return ToolExecutionResult(
                content=content,
                metadata={
                    "path": result.path,
                    "start_line": result.start_line,
                    "end_line": result.end_line,
                    "truncated": truncated,
                },
            )
        except (FileToolError, RuntimeError, ValueError) as exc:
            return self._error_result(exc)


class ReadManyFilesTool(BaseFileTool):
    spec = ToolSpec(
        name="read_many_files",
        description="Read multiple text files from the workspace in a single call.",
        input_schema={
            "type": "object",
            "properties": {
                "paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Paths to the files to read.",
                },
                "start_line": {"type": "integer", "description": "1-indexed first line to read."},
                "end_line": {"type": "integer", "description": "1-indexed last line to read."},
                "max_chars": {"type": "integer", "description": "Maximum number of characters to return."},
            },
            "required": ["paths"],
        },
    )

    async def execute(self, arguments: dict[str, Any], context: ToolExecutionContext) -> ToolExecutionResult:
        try:
            paths = [str(path) for path in arguments["paths"]]
            result = _require_workspace_files(context).read_many_files(
                paths=paths,
                start_line=max(int(arguments.get("start_line", 1)), 1),
                end_line=(
                    int(arguments["end_line"])
                    if arguments.get("end_line") is not None
                    else None
                ),
            )
            content, truncated = _truncate_text(
                self.formatter.format_multi_file_read(result),
                arguments.get("max_chars"),
            )
            return ToolExecutionResult(
                content=content,
                metadata={
                    "paths": [file_result.path for file_result in result.files],
                    "returned_count": len(result.files),
                    "truncated": truncated,
                },
            )
        except (FileToolError, RuntimeError, ValueError) as exc:
            return self._error_result(exc)


class ReplaceTool(BaseFileTool):
    spec = ToolSpec(
        name="replace",
        description="Replace exactly one matching text span in a workspace file.",
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to the file to modify."},
                "old_text": {"type": "string", "description": "Exact text to replace. Must match exactly once."},
                "new_text": {"type": "string", "description": "Replacement text."},
            },
            "required": ["path", "old_text", "new_text"],
        },
    )

    async def execute(self, arguments: dict[str, Any], context: ToolExecutionContext) -> ToolExecutionResult:
        try:
            result = _require_workspace_files(context).replace_exact(
                path=str(arguments["path"]),
                old_text=str(arguments["old_text"]),
                new_text=str(arguments["new_text"]),
            )
            return ToolExecutionResult(
                content=self.formatter.format_replace(result),
                metadata={
                    "path": result.path,
                    "match_count": result.match_count,
                    "bytes_written": result.bytes_written,
                },
            )
        except (FileToolError, RuntimeError, ValueError) as exc:
            return self._error_result(exc)


class WriteFileTool(BaseFileTool):
    spec = ToolSpec(
        name="write_file",
        description="Create or overwrite a text file in the workspace.",
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to the file to write."},
                "content": {"type": "string", "description": "Full file contents to write."},
                "if_exists": {
                    "type": "string",
                    "enum": ["overwrite", "error"],
                    "description": "What to do if the file already exists.",
                },
            },
            "required": ["path", "content"],
        },
    )

    async def execute(self, arguments: dict[str, Any], context: ToolExecutionContext) -> ToolExecutionResult:
        try:
            result = _require_workspace_files(context).write_file(
                path=str(arguments["path"]),
                content=str(arguments["content"]),
                if_exists=str(arguments.get("if_exists", "overwrite")),
            )
            return ToolExecutionResult(
                content=self.formatter.format_write_file(result),
                metadata={
                    "path": result.path,
                    "created": result.created,
                    "overwritten": result.overwritten,
                    "bytes_written": result.bytes_written,
                },
            )
        except (FileToolError, RuntimeError, ValueError) as exc:
            return self._error_result(exc)
