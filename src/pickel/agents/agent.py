from dataclasses import dataclass, field
from pathlib import Path

from pickel.agents.skills import (
    SkillManifest,
    SystemInstructionParts,
    compose_system_instruction,
    compose_system_instruction_parts,
)
from pickel.shared.model_config import ModelConfig


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
    # prepare 时若非空则每次 re-discover；否则用 skills 缓存
    skills_path: Path | None = None

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
