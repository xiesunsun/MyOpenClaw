from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path


class PathAccessPolicy(ABC):
    @abstractmethod
    def resolve(self, path: str, workspace_path: Path) -> Path:
        raise NotImplementedError

    @abstractmethod
    def assert_readable(self, path: Path) -> None:
        raise NotImplementedError

    @abstractmethod
    def assert_writable(self, path: Path) -> None:
        raise NotImplementedError


class WorkspacePathAccessPolicy(PathAccessPolicy):
    def resolve(self, path: str, workspace_path: Path) -> Path:
        candidate = Path(path)
        resolved_workspace = workspace_path.resolve()
        resolved_path = candidate.resolve() if candidate.is_absolute() else (resolved_workspace / candidate).resolve()
        try:
            resolved_path.relative_to(resolved_workspace)
        except ValueError as exc:
            raise PermissionError(f"Path '{path}' is outside the workspace") from exc
        return resolved_path

    def assert_readable(self, path: Path) -> None:
        if not path.exists():
            raise FileNotFoundError(f"File not found: {path}")
        if path.is_dir():
            raise IsADirectoryError(f"Path is a directory: {path}")

    def assert_writable(self, path: Path) -> None:
        parent = path.parent
        if not parent.exists():
            raise FileNotFoundError(f"Parent directory does not exist: {parent}")
        if parent.is_file():
            raise NotADirectoryError(f"Parent path is not a directory: {parent}")
