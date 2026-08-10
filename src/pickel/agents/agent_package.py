"""从 Pickel 设置解析出的 Agent 定义、不可变版本与进程内加载结果。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

from pickel.agents.agent import Agent
from pickel.agents.skills import SkillManifest
from pickel.tools.bus import ToolSnapshot


@dataclass(frozen=True)
class AgentDefinition:
    agent_id: str
    workspace_path: str
    behavior_path: str
    skills_path: str | None
    tool_ids: tuple[str, ...]
    extension_ids: tuple[str, ...]
    file_access_mode: str
    provider: str
    model: str


@dataclass(frozen=True)
class AgentSkillVersion:
    name: str
    description: str
    version: str
    status: str
    required_env: tuple[str, ...]
    allowed_tools: tuple[str, ...]
    source_path: str
    content: str
    digest: str


@dataclass(frozen=True)
class AgentToolVersion:
    name: str
    source: str
    version: str | None
    origin: str | None
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None


@dataclass(frozen=True)
class AgentModelVersion:
    provider: str
    model: str
    api_base: str | None
    temperature: float | None
    max_input_tokens: int | None
    max_output_tokens: int
    provider_options: dict[str, Any]
    required_secrets: tuple[str, ...]


@dataclass(frozen=True)
class AgentPackageVersion:
    package_version_id: str
    digest: str
    agent_id: str
    definition: AgentDefinition
    behavior_instruction: str
    model: AgentModelVersion
    skills: tuple[AgentSkillVersion, ...]
    tools: tuple[AgentToolVersion, ...]
    created_at: datetime

    def content_dict(self) -> dict[str, Any]:
        """返回参与 digest 和持久化的稳定内容，不包含创建时间。"""
        return {
            "schema_version": 1,
            "agent_id": self.agent_id,
            "definition": asdict(self.definition),
            "behavior_instruction": self.behavior_instruction,
            "model": asdict(self.model),
            "skills": [asdict(skill) for skill in self.skills],
            "tools": [asdict(tool) for tool in self.tools],
        }


@dataclass(frozen=True)
class LoadedAgentPackage:
    """不可持久化的运行期加载结果。"""

    definition: AgentDefinition
    version: AgentPackageVersion
    agent: Agent
    tool_snapshot: ToolSnapshot
    skill_manifests: tuple[SkillManifest, ...]


def agent_package_digest(content: dict[str, Any]) -> str:
    encoded = json.dumps(
        content,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def agent_package_version_from_content(
    *,
    package_version_id: str,
    digest: str,
    content: dict[str, Any],
    created_at: datetime,
) -> AgentPackageVersion:
    if content.get("schema_version") != 1:
        raise ValueError(
            f"不支持的 AgentPackageVersion schema: {content.get('schema_version')}"
        )
    definition_data = dict(content["definition"])
    definition_data["tool_ids"] = tuple(definition_data.get("tool_ids") or ())
    definition_data["extension_ids"] = tuple(definition_data.get("extension_ids") or ())
    model_data = dict(content["model"])
    model_data["required_secrets"] = tuple(model_data.get("required_secrets") or ())
    skills = []
    for item in content.get("skills") or []:
        data = dict(item)
        data["required_env"] = tuple(data.get("required_env") or ())
        data["allowed_tools"] = tuple(data.get("allowed_tools") or ())
        skills.append(AgentSkillVersion(**data))
    tools = tuple(AgentToolVersion(**dict(item)) for item in content.get("tools") or [])
    version = AgentPackageVersion(
        package_version_id=package_version_id,
        digest=digest,
        agent_id=str(content["agent_id"]),
        definition=AgentDefinition(**definition_data),
        behavior_instruction=str(content["behavior_instruction"]),
        model=AgentModelVersion(**model_data),
        skills=tuple(skills),
        tools=tools,
        created_at=created_at,
    )
    if agent_package_digest(version.content_dict()) != digest:
        raise ValueError(f"AgentPackageVersion digest 校验失败: {package_version_id}")
    return version
