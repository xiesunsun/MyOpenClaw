from dataclasses import dataclass
from pathlib import Path

from myopenclaw.llm.config import ModelConfig


@dataclass
class AgentDefinition:
    agent_id: str
    workspace_path: Path
    behavior_path: Path
    behavior_instruction: str
    model_config: ModelConfig
