from dataclasses import dataclass
from pathlib import Path

from myopenclaw.shared.model_config import ModelConfig


@dataclass
class Agent:
    agent_id: str
    workspace_path: Path
    behavior_path: Path
    behavior_instruction: str
    model_config: ModelConfig
    tool_ids: list[str]
    file_access_mode: str = "workspace"

    @property
    def system_instruction(self) -> str:
        return self.behavior_instruction

    @property
    def workspace(self):
        return self.workspace_path
