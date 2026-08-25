"""实际工作目录的长期身份。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


@dataclass(frozen=True)
class Workspace:
    workspace_id: str
    root_path: Path
    created_at: datetime

    def __post_init__(self) -> None:
        if not self.workspace_id:
            raise ValueError("workspace_id 不能为空")
        path = Path(self.root_path).expanduser().resolve(strict=False)
        object.__setattr__(self, "root_path", path)
