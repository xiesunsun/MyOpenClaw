from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from myopenclaw.tools.file_errors import (
    DirectoryNotReadableError,
    FileNotReadableError,
    FileNotWritableError,
    PathOutsideWorkspaceError,
)


class FileAccessPolicy(ABC):
    @abstractmethod
    def resolve_path(self, path: str, workspace_path: Path) -> Path:
        raise NotImplementedError

    @abstractmethod
    def assert_file_readable(self, path: Path) -> None:
        raise NotImplementedError

    @abstractmethod
    def assert_directory_readable(self, path: Path) -> None:
        raise NotImplementedError

    @abstractmethod
    def assert_file_writable(self, path: Path) -> None:
        raise NotImplementedError


class WorkspacePathAccessPolicy(FileAccessPolicy):
    def resolve_path(self, path: str, workspace_path: Path) -> Path:
        candidate = Path(path)
        resolved_workspace = workspace_path.resolve()
        resolved_path = candidate.resolve() if candidate.is_absolute() else (resolved_workspace / candidate).resolve()
        try:
            resolved_path.relative_to(resolved_workspace)
        except ValueError as exc:
            raise PathOutsideWorkspaceError(f"Path '{path}' is outside the workspace") from exc
        return resolved_path

    def assert_file_readable(self, path: Path) -> None:
        if not path.exists():
            raise FileNotReadableError(f"File not found: {path}")
        if path.is_dir():
            raise FileNotReadableError(f"Path is a directory: {path}")

    def assert_directory_readable(self, path: Path) -> None:
        if not path.exists():
            raise DirectoryNotReadableError(f"Directory not found: {path}")
        if not path.is_dir():
            raise DirectoryNotReadableError(f"Path is not a directory: {path}")

    def assert_file_writable(self, path: Path) -> None:
        parent = path.parent
        if not parent.exists():
            raise FileNotWritableError(f"Parent directory does not exist: {parent}")
        if parent.is_file():
            raise FileNotWritableError(f"Parent path is not a directory: {parent}")


class FullAccessPathPolicy(FileAccessPolicy):
    def resolve_path(self, path: str, workspace_path: Path) -> Path:
        candidate = Path(path)
        resolved_workspace = workspace_path.resolve()
        return candidate.resolve() if candidate.is_absolute() else (resolved_workspace / candidate).resolve()

    def assert_file_readable(self, path: Path) -> None:
        if not path.exists():
            raise FileNotReadableError(f"File not found: {path}")
        if path.is_dir():
            raise FileNotReadableError(f"Path is a directory: {path}")

    def assert_directory_readable(self, path: Path) -> None:
        if not path.exists():
            raise DirectoryNotReadableError(f"Directory not found: {path}")
        if not path.is_dir():
            raise DirectoryNotReadableError(f"Path is not a directory: {path}")

    def assert_file_writable(self, path: Path) -> None:
        parent = path.parent
        if not parent.exists():
            raise FileNotWritableError(f"Parent directory does not exist: {parent}")
        if parent.is_file():
            raise FileNotWritableError(f"Parent path is not a directory: {parent}")
