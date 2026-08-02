from __future__ import annotations

from pickel.tools.file_models import (
    DirectoryListing,
    FileReadResult,
    GlobSearchResult,
    GrepSearchResult,
    ReplaceResult,
    WriteFileResult,
)


class FileToolFormatter:
    def format_directory_listing(self, result: DirectoryListing) -> str:
        lines: list[str] = []
        for entry in result.entries:
            suffix = "/" if entry.entry_type == "directory" else ""
            lines.append(f"{entry.path}{suffix}")
        if result.truncated:
            lines.append("[Result limit reached. Use a narrower path.]")
        return "\n".join(lines) if lines else "(empty directory)"

    def format_glob_search(self, result: GlobSearchResult) -> str:
        lines: list[str] = []
        for match in result.matches:
            lines.append(match.path)
        if result.truncated:
            lines.append("[Result limit reached. Use a narrower pattern or path.]")
        return "\n".join(lines) if lines else "(no matches)"

    def format_grep_search(self, result: GrepSearchResult) -> str:
        lines: list[str] = []
        for hit in result.hits:
            lines.append(f"{hit.path}:{hit.line_number}:{hit.line_text}")
        if result.truncated:
            lines.append("[Result limit reached. Use a narrower pattern or path.]")
        return "\n".join(lines) if lines else "(no matches)"

    def format_file_read(self, result: FileReadResult) -> str:
        lines: list[str] = []
        for index, line in enumerate(result.lines, start=result.start_line):
            lines.append(f"{index}: {line}")
        if result.truncated:
            total = (
                f" of {result.total_lines}" if result.total_lines is not None else ""
            )
            lines.append(
                f"[Showing lines {result.start_line}-{result.end_line}{total}. "
                f"Use offset={result.end_line + 1} to continue.]"
            )
        return "\n".join(lines) if lines else "(empty file)"

    def format_replace(self, result: ReplaceResult) -> str:
        return result.diff or f"Edited {result.path}"

    def format_write_file(self, result: WriteFileResult) -> str:
        action = "created" if result.created else "overwritten"
        return f"{action} {result.path}"
