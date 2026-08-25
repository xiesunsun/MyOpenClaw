"""一次 Operation 冻结的工作区执行边界。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class WorkspaceBinding:
    workspace_id: str
    working_directory: Path
    allowed_root: Path | None

    def __post_init__(self) -> None:
        if not self.workspace_id:
            raise ValueError("workspace_id 不能为空")
        working_directory = (
            Path(self.working_directory).expanduser().resolve(strict=False)
        )
        allowed_root = (
            Path(self.allowed_root).expanduser().resolve(strict=False)
            if self.allowed_root is not None
            else None
        )
        if allowed_root is not None and not _is_within(working_directory, allowed_root):
            raise ValueError("working_directory 必须位于 allowed_root 内")
        object.__setattr__(self, "working_directory", working_directory)
        object.__setattr__(self, "allowed_root", allowed_root)


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True
