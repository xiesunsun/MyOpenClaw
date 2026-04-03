from __future__ import annotations

from myopenclaw.tools.file_models import (
    DirectoryListing,
    FileReadResult,
    GlobSearchResult,
    GrepSearchResult,
    MultiFileReadResult,
    ReplaceResult,
    WriteFileResult,
)


class FileToolFormatter:
    def format_directory_listing(self, result: DirectoryListing) -> str:
        lines = [f"Directory listing for {result.base_path}:"]
        for entry in result.entries:
            suffix = "/" if entry.entry_type == "directory" else ""
            lines.append(f"- {entry.path}{suffix}")
        if result.truncated:
            lines.append("[truncated]")
        return "\n".join(lines)

    def format_glob_search(self, result: GlobSearchResult) -> str:
        lines = [f"Glob matches for {result.pattern!r} in {result.base_path}:"]
        for match in result.matches:
            lines.append(f"- {match.path}")
        if result.truncated:
            lines.append("[truncated]")
        return "\n".join(lines)

    def format_grep_search(self, result: GrepSearchResult) -> str:
        lines = [f"Search hits for {result.pattern!r}:"]
        for hit in result.hits:
            lines.append(f"{hit.path}:{hit.line_number}: {hit.line_text}")
        if result.truncated:
            lines.append("[truncated]")
        return "\n".join(lines)

    def format_file_read(self, result: FileReadResult) -> str:
        lines = [f"File: {result.path}"]
        for index, line in enumerate(result.lines, start=result.start_line):
            lines.append(f"{index}: {line}")
        if result.truncated:
            lines.append("[truncated]")
        return "\n".join(lines)

    def format_multi_file_read(self, result: MultiFileReadResult) -> str:
        chunks: list[str] = []
        for file_result in result.files:
            chunks.append(self.format_file_read(file_result))
        if result.truncated:
            chunks.append("[truncated]")
        return "\n\n".join(chunks)

    def format_replace(self, result: ReplaceResult) -> str:
        return f"Replaced {result.match_count} exact match in {result.path}"

    def format_write_file(self, result: WriteFileResult) -> str:
        action = "created" if result.created else "overwritten"
        return f"{action} {result.path}"
