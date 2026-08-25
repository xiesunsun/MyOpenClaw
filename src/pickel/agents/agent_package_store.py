"""Agent Package Version 的窄持久化接口。"""

from __future__ import annotations

from typing import Protocol

from pickel.agents.agent_package import AgentPackageVersion


class AgentPackageVersionStore(Protocol):
    def insert_agent_package_version(self, version: AgentPackageVersion) -> None: ...

    def load_agent_package_version(
        self, package_version_id: str
    ) -> AgentPackageVersion | None: ...
