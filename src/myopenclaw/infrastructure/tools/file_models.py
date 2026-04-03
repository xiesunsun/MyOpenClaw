from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


@dataclass(frozen=True)
class DirectoryEntry:
    path: str
    entry_type: Literal["file", "directory"]
    size_bytes: int | None = None


@dataclass(frozen=True)
class DirectoryListing:
    base_path: str
    entries: list[DirectoryEntry] = field(default_factory=list)
    truncated: bool = False


@dataclass(frozen=True)
class GlobMatch:
    path: str


@dataclass(frozen=True)
class GlobSearchResult:
    base_path: str
    pattern: str
    matches: list[GlobMatch] = field(default_factory=list)
    truncated: bool = False


@dataclass(frozen=True)
class SearchHit:
    path: str
    line_number: int
    line_text: str


@dataclass(frozen=True)
class GrepSearchResult:
    pattern: str
    hits: list[SearchHit] = field(default_factory=list)
    truncated: bool = False


@dataclass(frozen=True)
class FileReadResult:
    path: str
    start_line: int
    end_line: int
    lines: list[str] = field(default_factory=list)
    truncated: bool = False


@dataclass(frozen=True)
class MultiFileReadResult:
    files: list[FileReadResult] = field(default_factory=list)
    truncated: bool = False


@dataclass(frozen=True)
class ReplaceResult:
    path: str
    match_count: int
    bytes_written: int


@dataclass(frozen=True)
class WriteFileResult:
    path: str
    created: bool
    overwritten: bool
    bytes_written: int
