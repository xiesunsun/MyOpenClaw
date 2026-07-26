from __future__ import annotations

from pydantic import BaseModel, Field


class OpenVikingAgentConfig(BaseModel):
    remote_agent_id: str
    enabled: bool = True


class OpenVikingSessionRecallConfig(BaseModel):
    enabled: bool = True
    max_chars: int = 6000
    limit: int = 5
    min_score: float | None = None


class OpenVikingConfig(BaseModel):
    enabled: bool = False
    base_url: str
    account_id: str
    user_id: str
    user_key: str
    timeout_seconds: float = 30.0
    commit_after_minutes: int = 30
    commit_after_turns: int = 8
    tool_output_max_chars: int = 4000
    session_recall: OpenVikingSessionRecallConfig = Field(
        default_factory=OpenVikingSessionRecallConfig
    )
    agents: dict[str, OpenVikingAgentConfig] = Field(default_factory=dict)
