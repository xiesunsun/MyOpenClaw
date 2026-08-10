"""只从 Pickel 现有设置构建 Agent Package。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pickel.agents.agent import Agent
from pickel.agents.agent_package import (
    AgentDefinition,
    AgentModelVersion,
    AgentPackageVersion,
    AgentSkillVersion,
    AgentToolVersion,
    LoadedAgentPackage,
    agent_package_digest,
)
from pickel.agents.behavior_loader import BehaviorLoader
from pickel.agents.skills import SkillManifest, SkillRegistry
from pickel.config.app_config import AppConfig
from pickel.shared.file_access import FileAccessMode
from pickel.shared.model_config import ModelConfig
from pickel.tools.bus import ToolActivation, ToolBus, ToolSnapshot

_SECRET_MARKERS = ("api_key", "token", "secret", "password", "authorization")


@dataclass(frozen=True)
class _AgentPackageDigestInput:
    schema_version: int
    agent_id: str
    definition: AgentDefinition
    behavior_instruction: str
    model: AgentModelVersion
    skills: tuple[AgentSkillVersion, ...]
    tools: tuple[AgentToolVersion, ...]


class AgentPackageBuilder:
    """解析 AppConfig/AgentConfig，并冻结为可恢复的版本。"""

    def __init__(
        self,
        *,
        app_config: AppConfig,
        tool_bus: ToolBus,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._app_config = app_config
        self._tool_bus = tool_bus
        self._now = now or (lambda: datetime.now(timezone.utc))

    def build_loaded_agent_package(
        self,
        agent_id: str | None = None,
    ) -> LoadedAgentPackage:
        resolved_agent_id = agent_id or self._app_config.default_agent
        agent_config = self._app_config.get_agent_config(resolved_agent_id)
        behavior_instruction = BehaviorLoader.load(agent_config.behavior_path)
        file_access_mode = self._app_config.resolve_file_access_mode(resolved_agent_id)
        skills_path = self.resolve_skills_path(resolved_agent_id)
        skill_manifests = tuple(SkillRegistry.discover(skills_path))
        model_config = self._app_config.resolve_model_config(agent_config.llm)
        tool_snapshot = self._tool_bus.snapshot(
            ToolActivation(allowed=frozenset(agent_config.tools))
        )
        definition = AgentDefinition(
            agent_id=resolved_agent_id,
            workspace_path=str(agent_config.workspace_path),
            behavior_path=str(agent_config.behavior_path),
            skills_path=str(skills_path) if skills_path is not None else None,
            tool_ids=tuple(agent_config.tools),
            extension_ids=tuple(agent_config.extensions),
            file_access_mode=file_access_mode.value,
            provider=model_config.provider,
            model=model_config.model,
        )
        agent = Agent(
            agent_id=resolved_agent_id,
            workspace_path=agent_config.workspace_path,
            behavior_path=agent_config.behavior_path,
            behavior_instruction=behavior_instruction,
            model_config=model_config,
            tool_ids=list(agent_config.tools),
            file_access_mode=file_access_mode.value,
            skills=list(skill_manifests),
            skills_path=skills_path,
        )
        version = self._build_version(
            definition=definition,
            behavior_instruction=behavior_instruction,
            skill_manifests=skill_manifests,
            tool_snapshot=tool_snapshot,
            model_config=model_config,
        )
        return LoadedAgentPackage(
            definition=definition,
            version=version,
            agent=agent,
            tool_snapshot=tool_snapshot,
            skill_manifests=skill_manifests,
        )

    def resolve_skills_path(self, agent_id: str) -> Path | None:
        agent_config = self._app_config.get_agent_config(agent_id)
        skills_path = self._app_config.resolve_skills_path(agent_id)
        if skills_path is None:
            return None
        if (
            skills_path.exists()
            and self._app_config.resolve_file_access_mode(agent_id)
            != FileAccessMode.FULL
            and not self._is_within_workspace(skills_path, agent_config.workspace_path)
        ):
            raise ValueError(
                f"Skills path '{skills_path}' is outside workspace "
                f"'{agent_config.workspace_path}' and requires file_access_mode: full"
            )
        return skills_path

    def _build_version(
        self,
        *,
        definition: AgentDefinition,
        behavior_instruction: str,
        skill_manifests: tuple[SkillManifest, ...],
        tool_snapshot: ToolSnapshot,
        model_config: ModelConfig,
    ) -> AgentPackageVersion:
        model = AgentModelVersion(
            provider=model_config.provider,
            model=model_config.model,
            api_base=model_config.api_base,
            temperature=model_config.temperature,
            max_input_tokens=model_config.max_input_tokens,
            max_output_tokens=model_config.max_output_tokens,
            provider_options=self._remove_secrets(model_config.provider_options),
            required_secrets=("api_key",) if model_config.api_key else (),
        )
        skills = tuple(self._skill_version(manifest) for manifest in skill_manifests)
        tools = tuple(
            AgentToolVersion(
                name=entry.name,
                source=entry.source.value,
                version=entry.version,
                origin=entry.origin,
                description=entry.tool.spec.description,
                input_schema=self._json_copy(entry.tool.spec.input_schema),
                output_schema=(
                    self._json_copy(entry.tool.spec.output_schema)
                    if entry.tool.spec.output_schema is not None
                    else None
                ),
            )
            for entry in tool_snapshot.entries
        )
        draft = {
            "schema_version": 1,
            "agent_id": definition.agent_id,
            "definition": definition,
            "behavior_instruction": behavior_instruction,
            "model": model,
            "skills": skills,
            "tools": tools,
        }
        stable = asdict(_AgentPackageDigestInput(**draft))
        digest = agent_package_digest(stable)
        return AgentPackageVersion(
            package_version_id=f"agentpkg_{digest}",
            digest=digest,
            agent_id=definition.agent_id,
            definition=definition,
            behavior_instruction=behavior_instruction,
            model=model,
            skills=skills,
            tools=tools,
            created_at=self._now(),
        )

    @staticmethod
    def _skill_version(manifest: SkillManifest) -> AgentSkillVersion:
        content = manifest.skill_file.read_text(encoding="utf-8")
        return AgentSkillVersion(
            name=manifest.name,
            description=manifest.description,
            version=manifest.version,
            status=manifest.status,
            required_env=manifest.required_env,
            allowed_tools=manifest.allowed_tools,
            source_path=str(manifest.skill_file),
            content=content,
            digest=hashlib.sha256(content.encode("utf-8")).hexdigest(),
        )

    @classmethod
    def _remove_secrets(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: cls._remove_secrets(item)
                for key, item in value.items()
                if not any(marker in key.lower() for marker in _SECRET_MARKERS)
            }
        if isinstance(value, list):
            return [cls._remove_secrets(item) for item in value]
        return value

    @staticmethod
    def _json_copy(value: dict[str, Any]) -> dict[str, Any]:
        return json.loads(json.dumps(value, ensure_ascii=False))

    @staticmethod
    def _is_within_workspace(path: Path, workspace_path: Path) -> bool:
        try:
            path.resolve().relative_to(workspace_path.resolve())
        except ValueError:
            return False
        return True
