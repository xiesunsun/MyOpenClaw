"""Workspace 的独立持久化窄接口。"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from pickel.workspaces.workspace import Workspace


class WorkspaceStore(Protocol):
    def load_workspace(self, workspace_id: str) -> Workspace | None: ...

    def find_workspace_by_root(self, root_path: str | Path) -> Workspace | None: ...
