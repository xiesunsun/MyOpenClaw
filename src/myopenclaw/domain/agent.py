from dataclasses import dataclass
from pathlib import Path


@dataclass
class Agent:
    agent_id: str
    workspace_path: Path
    behavior_instruction: str
    model_provider: str
    model_name: str
    tool_ids: list[str]
    behavior_path: Path | None = None
    file_access_mode: str = "workspace"

    @property
    def system_instruction(self) -> str:
        return self.behavior_instruction

    @property
    def workspace(self) -> Path:
        return self.workspace_path
