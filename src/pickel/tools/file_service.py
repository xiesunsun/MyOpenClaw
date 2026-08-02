from __future__ import annotations

import difflib
import fnmatch
import json
import re
import shutil
import subprocess
from pathlib import Path
from tempfile import NamedTemporaryFile

from pickel.tools.file_errors import (
    FileNotReadableError,
    FileNotWritableError,
    MultipleReplacementMatchesError,
    NoReplacementMatchError,
)
from pickel.tools.file_models import (
    DirectoryEntry,
    DirectoryListing,
    FileReadResult,
    GlobMatch,
    GlobSearchResult,
    GrepSearchResult,
    ReplaceResult,
    SearchHit,
    WriteFileResult,
)
from pickel.tools.policy import FileAccessPolicy


def _write_text_atomic(path: Path, content: str) -> None:
    with NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False
    ) as handle:
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
            if not include_hidden and any(
                part.startswith(".") for part in entry.relative_to(directory_path).parts
            ):
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

        rg_path = shutil.which("rg")
        if rg_path is not None:
            matches, truncated = self._glob_with_ripgrep(
                rg_path=rg_path,
                directory_path=directory_path,
                pattern=pattern,
                max_results=max_results,
            )
        else:
            matches, truncated = self._glob_with_python(
                directory_path=directory_path,
                pattern=pattern,
                max_results=max_results,
            )

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
        try:
            compiled = re.compile(pattern, flags)
        except re.error as exc:
            raise ValueError(f"Invalid regular expression: {exc}") from exc
        rg_path = shutil.which("rg")
        if rg_path is not None:
            hits, truncated = self._grep_with_ripgrep(
                rg_path=rg_path,
                directory_path=directory_path,
                pattern=pattern,
                glob_pattern=glob_pattern,
                case_sensitive=case_sensitive,
                max_results=max_results,
            )
        else:
            hits, truncated = self._grep_with_python(
                directory_path=directory_path,
                compiled=compiled,
                glob_pattern=glob_pattern,
                max_results=max_results,
            )

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
        normalized_start = max(start_line, 1)
        normalized_end = (
            None if end_line is None else max(end_line, normalized_start - 1)
        )
        selected_lines: list[str] = []
        total_lines: int | None = 0
        truncated = False
        try:
            with file_path.open("r", encoding=self.default_encoding) as handle:
                for total_lines, line in enumerate(handle, start=1):
                    if total_lines < normalized_start:
                        continue
                    if normalized_end is not None and total_lines > normalized_end:
                        truncated = True
                        total_lines = None
                        break
                    selected_lines.append(line.rstrip("\r\n"))
        except UnicodeDecodeError as exc:
            raise FileNotReadableError(
                f"File is not valid UTF-8 text: {file_path}"
            ) from exc

        if (
            total_lines is not None
            and normalized_start > total_lines
            and not (total_lines == 0 and normalized_start == 1)
        ):
            raise FileNotReadableError(
                f"Offset {normalized_start} is beyond the end of "
                f"{self._to_workspace_relative(file_path)} ({total_lines} lines)"
            )
        return FileReadResult(
            path=self._to_workspace_relative(file_path),
            start_line=normalized_start,
            end_line=normalized_start + max(len(selected_lines) - 1, 0),
            total_lines=total_lines,
            lines=selected_lines,
            truncated=truncated,
        )

    def _glob_with_ripgrep(
        self,
        *,
        rg_path: str,
        directory_path: Path,
        pattern: str,
        max_results: int,
    ) -> tuple[list[GlobMatch], bool]:
        process = subprocess.Popen(
            [rg_path, "--files", "--hidden", "--glob", pattern],
            cwd=directory_path,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding=self.default_encoding,
        )
        matches: list[GlobMatch] = []
        assert process.stdout is not None
        try:
            for raw_path in process.stdout:
                candidate = self.access_policy.resolve_path(
                    str(directory_path / raw_path.rstrip("\r\n")),
                    self.workspace_root,
                )
                self.access_policy.assert_file_readable(candidate)
                if len(matches) == max_results:
                    return matches, True
                matches.append(GlobMatch(path=self._to_workspace_relative(candidate)))

            stderr = process.stderr.read() if process.stderr is not None else ""
            return_code = process.wait()
            if return_code not in (0, 1):
                raise ValueError(
                    stderr.strip() or f"rg exited with status {return_code}"
                )
            return matches, False
        finally:
            _terminate_process(process)

    def _glob_with_python(
        self, *, directory_path: Path, pattern: str, max_results: int
    ) -> tuple[list[GlobMatch], bool]:
        matches: list[GlobMatch] = []
        for candidate in sorted(
            directory_path.glob(pattern), key=lambda item: item.as_posix()
        ):
            if not candidate.is_file():
                continue
            resolved = self.access_policy.resolve_path(
                str(candidate), self.workspace_root
            )
            if len(matches) == max_results:
                return matches, True
            matches.append(GlobMatch(path=self._to_workspace_relative(resolved)))
        return matches, False

    def _grep_with_ripgrep(
        self,
        *,
        rg_path: str,
        directory_path: Path,
        pattern: str,
        glob_pattern: str | None,
        case_sensitive: bool,
        max_results: int,
    ) -> tuple[list[SearchHit], bool]:
        command = [rg_path, "--json", "--line-number", "--color", "never"]
        if not case_sensitive:
            command.append("--ignore-case")
        if glob_pattern is not None:
            command.extend(["--glob", glob_pattern])
        command.extend(["--", pattern, "."])
        process = subprocess.Popen(
            command,
            cwd=directory_path,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding=self.default_encoding,
        )
        hits: list[SearchHit] = []
        assert process.stdout is not None
        try:
            for raw_event in process.stdout:
                event = json.loads(raw_event)
                if event.get("type") != "match":
                    continue
                data = event["data"]
                candidate = self.access_policy.resolve_path(
                    str(directory_path / data["path"]["text"]),
                    self.workspace_root,
                )
                self.access_policy.assert_file_readable(candidate)
                if len(hits) == max_results:
                    return hits, True
                hits.append(
                    SearchHit(
                        path=self._to_workspace_relative(candidate),
                        line_number=int(data["line_number"]),
                        line_text=data["lines"]["text"].rstrip("\r\n"),
                    )
                )

            stderr = process.stderr.read() if process.stderr is not None else ""
            return_code = process.wait()
            if return_code not in (0, 1):
                raise ValueError(
                    stderr.strip() or f"rg exited with status {return_code}"
                )
            return hits, False
        finally:
            _terminate_process(process)

    def _grep_with_python(
        self,
        *,
        directory_path: Path,
        compiled: re.Pattern[str],
        glob_pattern: str | None,
        max_results: int,
    ) -> tuple[list[SearchHit], bool]:
        hits: list[SearchHit] = []
        for candidate in sorted(
            directory_path.rglob("*"), key=lambda item: item.as_posix()
        ):
            if not candidate.is_file() or candidate.is_symlink():
                continue
            if glob_pattern is not None and not fnmatch.fnmatch(
                candidate.name, glob_pattern
            ):
                continue
            resolved = self.access_policy.resolve_path(
                str(candidate), self.workspace_root
            )
            self.access_policy.assert_file_readable(resolved)
            try:
                with resolved.open("r", encoding=self.default_encoding) as handle:
                    for line_number, line_text in enumerate(handle, start=1):
                        if not compiled.search(line_text):
                            continue
                        if len(hits) == max_results:
                            return hits, True
                        hits.append(
                            SearchHit(
                                path=self._to_workspace_relative(resolved),
                                line_number=line_number,
                                line_text=line_text.rstrip("\r\n"),
                            )
                        )
            except UnicodeDecodeError:
                continue
        return hits, False

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
            raise NoReplacementMatchError(
                f"No exact match found in {self._to_workspace_relative(file_path)}"
            )
        if match_count > 1:
            raise MultipleReplacementMatchesError(
                f"Found {match_count} exact matches in {self._to_workspace_relative(file_path)}"
            )

        next_content = current_content.replace(old_text, new_text, 1)
        relative_path = self._to_workspace_relative(file_path)
        diff = "".join(
            difflib.unified_diff(
                current_content.splitlines(keepends=True),
                next_content.splitlines(keepends=True),
                fromfile=relative_path,
                tofile=relative_path,
                n=3,
            )
        )
        _write_text_atomic(file_path, next_content)
        return ReplaceResult(
            path=relative_path,
            match_count=1,
            bytes_written=len(next_content.encode(self.default_encoding)),
            diff=diff,
        )

    def write_file(
        self,
        *,
        path: str,
        content: str,
    ) -> WriteFileResult:
        file_path = self.access_policy.resolve_path(path, self.workspace_root)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        self.access_policy.assert_file_writable(file_path)
        if file_path.exists() and file_path.is_dir():
            raise FileNotWritableError(f"Path is a directory: {file_path}")

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


def _terminate_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
