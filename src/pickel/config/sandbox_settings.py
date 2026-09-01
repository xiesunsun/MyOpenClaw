"""进程级沙箱配置。"""

from __future__ import annotations

from pydantic import BaseModel, Field


class SandboxSettings(BaseModel):
    enabled: bool = True
    strict: bool = False
    allow_write: list[str] = Field(default_factory=list)
    deny_read: list[str] = Field(default_factory=list)
    env_deny: list[str] = Field(default_factory=list)
    env_allow: list[str] = Field(default_factory=list)
