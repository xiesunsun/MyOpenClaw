from dataclasses import dataclass, field
from pathlib import Path

from myopenclaw.agents.skills import (
    SkillManifest,
    SystemInstructionParts,
    compose_system_instruction,
    compose_system_instruction_parts,
)
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
    skills: list[SkillManifest] = field(default_factory=list)

    @property
    def system_instruction(self) -> str:
        return compose_system_instruction(self.behavior_instruction, self.skills)

    @property
    def instruction_parts(self) -> SystemInstructionParts:
        return compose_system_instruction_parts(
            self.behavior_instruction,
            self.skills,
        )

    @property
    def workspace(self):
        return self.workspace_path
