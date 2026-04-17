from __future__ import annotations

from pathlib import Path
from tempfile import NamedTemporaryFile
import fnmatch
import re

from myopenclaw.tools.file_errors import (
    FileNotWritableError,
    MultipleReplacementMatchesError,
    NoReplacementMatchError,
)
from myopenclaw.tools.file_models import (
    DirectoryEntry,
    DirectoryListing,
    FileReadResult,
    GlobMatch,
    GlobSearchResult,
    GrepSearchResult,
    MultiFileReadResult,
    ReplaceResult,
    SearchHit,
    WriteFileResult,
)
from myopenclaw.tools.policy import FileAccessPolicy


def _write_text_atomic(path: Path, content: str) -> None:
    with NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        handle.write(content)
        temp_path = Path(handle.name)
    temp_path.replace(path)


class WorkspaceFileService:
    def __init__(
        self,
        *,
        workspace_root: Path,
        access_policy: FileAccessPolicy,
        default_encoding: str = "utf-8",
    ) -> None:
        self.workspace_root = workspace_root.resolve()
        self.access_policy = access_policy
        self.default_encoding = default_encoding

    def list_directory(
        self,
        *,
        path: str = ".",
        recursive: bool = False,
        include_hidden: bool = False,
        max_entries: int = 200,
    ) -> DirectoryListing:
        directory_path = self.access_policy.resolve_path(path, self.workspace_root)
        self.access_policy.assert_directory_readable(directory_path)

        entries: list[DirectoryEntry] = []
        truncated = False
        iterator = directory_path.rglob("*") if recursive else directory_path.iterdir()
        for entry in sorted(iterator, key=lambda candidate: candidate.as_posix()):
            if not include_hidden and any(part.startswith(".") for part in entry.relative_to(directory_path).parts):
                continue
            entries.append(
                DirectoryEntry(
                    path=self._to_workspace_relative(entry),
                    entry_type="directory" if entry.is_dir() else "file",
                    size_bytes=entry.stat().st_size if entry.is_file() else None,
                )
            )
            if len(entries) >= max_entries:
                truncated = True
                break

        return DirectoryListing(
            base_path=self._to_workspace_relative(directory_path),
            entries=entries,
            truncated=truncated,
        )

    def glob_search(
        self,
        *,
        pattern: str,
        base_path: str = ".",
        max_results: int = 200,
    ) -> GlobSearchResult:
        directory_path = self.access_policy.resolve_path(base_path, self.workspace_root)
        self.access_policy.assert_directory_readable(directory_path)

        matches: list[GlobMatch] = []
        truncated = False
        for candidate in sorted(directory_path.glob(pattern), key=lambda candidate: candidate.as_posix()):
            matches.append(GlobMatch(path=self._to_workspace_relative(candidate)))
            if len(matches) >= max_results:
                truncated = True
                break

        return GlobSearchResult(
            base_path=self._to_workspace_relative(directory_path),
            pattern=pattern,
            matches=matches,
            truncated=truncated,
        )

    def grep_search(
        self,
        *,
        pattern: str,
        base_path: str = ".",
        glob_pattern: str | None = None,
        case_sensitive: bool = False,
        max_results: int = 100,
    ) -> GrepSearchResult:
        directory_path = self.access_policy.resolve_path(base_path, self.workspace_root)
        self.access_policy.assert_directory_readable(directory_path)

        flags = 0 if case_sensitive else re.IGNORECASE
        compiled = re.compile(pattern, flags)
        hits: list[SearchHit] = []
        truncated = False

        for candidate in sorted(directory_path.rglob("*"), key=lambda candidate: candidate.as_posix()):
            if not candidate.is_file():
                continue
            if glob_pattern is not None and not fnmatch.fnmatch(candidate.name, glob_pattern):
                continue
            self.access_policy.assert_file_readable(candidate)
            try:
                lines = candidate.read_text(encoding=self.default_encoding).splitlines()
            except UnicodeDecodeError:
                continue

            for line_number, line_text in enumerate(lines, start=1):
                if not compiled.search(line_text):
                    continue
                hits.append(
                    SearchHit(
                        path=self._to_workspace_relative(candidate),
                        line_number=line_number,
                        line_text=line_text,
                    )
                )
                if len(hits) >= max_results:
                    truncated = True
                    break
            if truncated:
                break

        return GrepSearchResult(pattern=pattern, hits=hits, truncated=truncated)

    def read_file(
        self,
        *,
        path: str,
        start_line: int = 1,
        end_line: int | None = None,
    ) -> FileReadResult:
        file_path = self.access_policy.resolve_path(path, self.workspace_root)
        self.access_policy.assert_file_readable(file_path)
        lines = file_path.read_text(encoding=self.default_encoding).splitlines()
        normalized_start = max(start_line, 1)
        normalized_end = len(lines) if end_line is None else max(end_line, normalized_start - 1)
        selected_lines = lines[normalized_start - 1:normalized_end]
        return FileReadResult(
            path=self._to_workspace_relative(file_path),
            start_line=normalized_start,
            end_line=normalized_start + max(len(selected_lines) - 1, 0),
            lines=selected_lines,
        )

    def read_many_files(
        self,
        *,
        paths: list[str],
        start_line: int = 1,
        end_line: int | None = None,
    ) -> MultiFileReadResult:
        return MultiFileReadResult(
            files=[
                self.read_file(path=path, start_line=start_line, end_line=end_line)
                for path in paths
            ]
        )

    def replace_exact(
        self,
        *,
        path: str,
        old_text: str,
        new_text: str,
    ) -> ReplaceResult:
        if not old_text:
            raise FileNotWritableError("old_text must not be empty")

        file_path = self.access_policy.resolve_path(path, self.workspace_root)
        self.access_policy.assert_file_writable(file_path)
        if not file_path.exists():
            raise FileNotWritableError(f"File not found: {file_path}")
        self.access_policy.assert_file_readable(file_path)

        current_content = file_path.read_text(encoding=self.default_encoding)
        match_count = current_content.count(old_text)
        if match_count == 0:
            raise NoReplacementMatchError(f"No exact match found in {self._to_workspace_relative(file_path)}")
        if match_count > 1:
            raise MultipleReplacementMatchesError(
                f"Found {match_count} exact matches in {self._to_workspace_relative(file_path)}"
            )

        next_content = current_content.replace(old_text, new_text, 1)
        _write_text_atomic(file_path, next_content)
        return ReplaceResult(
            path=self._to_workspace_relative(file_path),
            match_count=1,
            bytes_written=len(next_content.encode(self.default_encoding)),
        )

    def write_file(
        self,
        *,
        path: str,
        content: str,
        if_exists: str = "overwrite",
    ) -> WriteFileResult:
        file_path = self.access_policy.resolve_path(path, self.workspace_root)
        self.access_policy.assert_file_writable(file_path)
        if file_path.exists() and file_path.is_dir():
            raise FileNotWritableError(f"Path is a directory: {file_path}")
        if file_path.exists() and if_exists == "error":
            raise FileNotWritableError(f"File already exists: {file_path}")
        if if_exists not in {"overwrite", "error"}:
            raise FileNotWritableError(f"Unsupported if_exists mode: {if_exists}")

        existed = file_path.exists()
        _write_text_atomic(file_path, content)
        return WriteFileResult(
            path=self._to_workspace_relative(file_path),
            created=not existed,
            overwritten=existed,
            bytes_written=len(content.encode(self.default_encoding)),
        )

    def _to_workspace_relative(self, path: Path) -> str:
        resolved_path = path.resolve()
        if resolved_path == self.workspace_root:
            return "."
        try:
            return resolved_path.relative_to(self.workspace_root).as_posix()
        except ValueError:
            return resolved_path.as_posix()
