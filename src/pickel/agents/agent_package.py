"""Agent Package 的不可变执行合同。"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Literal, Mapping, TypeAlias

from pickel.shared.frozen_json import FrozenJSON, freeze_json, thaw_json
from pickel.tools.bus import ToolSnapshot, ToolSource

if TYPE_CHECKING:
    from pickel.providers.base import Provider

_PACKAGE_ID = re.compile(r"^agentpkg_[0-9a-f]{64}$")
ModelRole: TypeAlias = Literal["primary", "worker", "utility"]


@dataclass(frozen=True)
class ImplementationRef:
    """可执行贡献的稳定定位；不承诺能够从 digest 恢复代码。"""

    kind: str
    name: str
    version: str | None = None
    digest: str | None = None

    def __post_init__(self) -> None:
        if not self.kind.strip() or not self.name.strip():
            raise ValueError("ImplementationRef.kind/name 不能为空")
        if self.digest is not None and not re.fullmatch(r"[0-9a-f]{64}", self.digest):
            raise ValueError("ImplementationRef.digest 必须是 64 位小写 SHA-256")


@dataclass(frozen=True)
class SecretRef:
    """秘密的逻辑引用；Package 永远不保存对应值。"""

    name: str

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("SecretRef.name 不能为空")


@dataclass(frozen=True)
class WorkspacePolicy:
    file_scope: Literal["workspace", "full"] = "workspace"

    def __post_init__(self) -> None:
        if self.file_scope not in {"workspace", "full"}:
            raise ValueError("WorkspacePolicy.file_scope 必须是 workspace 或 full")


@dataclass(frozen=True)
class AgentRuntimePolicy:
    max_model_steps: int
    context_turn_window: int
    max_delegation_depth: int = 3

    def __post_init__(self) -> None:
        if self.max_model_steps < 1:
            raise ValueError("max_model_steps 必须大于 0")
        if self.context_turn_window < 1:
            raise ValueError("context_turn_window 必须大于 0")
        if self.max_delegation_depth < 0:
            raise ValueError("max_delegation_depth 不能小于 0")


@dataclass(frozen=True)
class ModelVersion:
    provider: str
    model: str
    api_base: str | None
    temperature: float | None
    max_input_tokens: int | None
    max_output_tokens: int
    provider_options: Mapping[str, FrozenJSON]
    provider_implementation: ImplementationRef
    required_secret_refs: tuple[SecretRef, ...]

    def __post_init__(self) -> None:
        if not self.provider.strip() or not self.model.strip():
            raise ValueError("ModelVersion.provider/model 不能为空")
        if self.max_output_tokens < 1:
            raise ValueError("max_output_tokens 必须大于 0")
        object.__setattr__(self, "provider_options", freeze_json(self.provider_options))
        object.__setattr__(
            self, "required_secret_refs", tuple(self.required_secret_refs)
        )


@dataclass(frozen=True)
class ModelPolicy:
    primary: ModelVersion
    worker: ModelVersion | None = None
    utility: ModelVersion | None = None


@dataclass(frozen=True)
class SkillVersion:
    name: str
    version: str
    description: str
    content: str
    required_secret_refs: tuple[SecretRef, ...] = ()
    allowed_tools: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "required_secret_refs", tuple(self.required_secret_refs)
        )
        object.__setattr__(self, "allowed_tools", tuple(self.allowed_tools))


@dataclass(frozen=True)
class ToolVersion:
    name: str
    source: ToolSource
    implementation_ref: ImplementationRef
    version: str | None
    description: str
    input_schema: Mapping[str, FrozenJSON]
    output_schema: Mapping[str, FrozenJSON] | None
    replay_policy: Literal["safe", "never"]

    def __post_init__(self) -> None:
        if self.replay_policy not in {"safe", "never"}:
            raise ValueError("ToolVersion.replay_policy 必须是 safe 或 never")
        object.__setattr__(self, "source", ToolSource(self.source))
        object.__setattr__(self, "input_schema", freeze_json(self.input_schema))
        if self.output_schema is not None:
            object.__setattr__(self, "output_schema", freeze_json(self.output_schema))


@dataclass(frozen=True)
class ExtensionVersion:
    extension_id: str
    implementation_ref: ImplementationRef
    version: str | None
    config: Mapping[str, FrozenJSON]
    required_secret_refs: tuple[SecretRef, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "config", freeze_json(self.config))
        object.__setattr__(
            self, "required_secret_refs", tuple(self.required_secret_refs)
        )


@dataclass(frozen=True)
class AgentDefinition:
    """配置解析结果，不进入 AgentPackageVersion。"""

    agent_id: str
    default_workspace_path: Path
    workspace_policy: WorkspacePolicy
    behavior_path: Path
    skills_path: Path | None
    allowed_tools: tuple[str, ...]
    extension_ids: tuple[str, ...]
    model_policy: ModelPolicy
    runtime_policy: AgentRuntimePolicy

    def __post_init__(self) -> None:
        object.__setattr__(self, "allowed_tools", tuple(self.allowed_tools))
        object.__setattr__(self, "extension_ids", tuple(self.extension_ids))


@dataclass(frozen=True)
class AgentPackageVersion:
    package_version_id: str
    agent_id: str
    format_version: int
    behavior_instruction: str
    model_policy: ModelPolicy
    runtime_policy: AgentRuntimePolicy
    workspace_policy: WorkspacePolicy
    skills: tuple[SkillVersion, ...]
    tools: tuple[ToolVersion, ...]
    extensions: tuple[ExtensionVersion, ...]
    created_at: datetime

    def __post_init__(self) -> None:
        if self.format_version < 1:
            raise ValueError("format_version 必须大于 0")
        if not self.agent_id.strip():
            raise ValueError("agent_id 不能为空")
        if not _PACKAGE_ID.fullmatch(self.package_version_id):
            raise ValueError("package_version_id 必须是 agentpkg_<sha256>")
        object.__setattr__(self, "skills", tuple(self.skills))
        object.__setattr__(self, "tools", tuple(self.tools))
        object.__setattr__(self, "extensions", tuple(self.extensions))
        if self.package_version_id != package_version_id_for_content(
            self.content_dict()
        ):
            raise ValueError("package_version_id 与 canonical Package 内容不一致")

    def content_dict(self) -> dict[str, Any]:
        """返回不含创建时间的 JSON-compatible canonical 内容。"""
        return _content_dict(
            format_version=self.format_version,
            agent_id=self.agent_id,
            behavior_instruction=self.behavior_instruction,
            model_policy=self.model_policy,
            runtime_policy=self.runtime_policy,
            workspace_policy=self.workspace_policy,
            skills=self.skills,
            tools=self.tools,
            extensions=self.extensions,
        )


def build_agent_package_version(
    *,
    agent_id: str,
    format_version: int,
    behavior_instruction: str,
    model_policy: ModelPolicy,
    runtime_policy: AgentRuntimePolicy,
    workspace_policy: WorkspacePolicy,
    skills: tuple[SkillVersion, ...],
    tools: tuple[ToolVersion, ...],
    extensions: tuple[ExtensionVersion, ...],
    created_at: datetime,
) -> AgentPackageVersion:
    """通过唯一 canonical codec 创建 Package。"""
    content = _content_dict(
        format_version=format_version,
        agent_id=agent_id,
        behavior_instruction=behavior_instruction,
        model_policy=model_policy,
        runtime_policy=runtime_policy,
        workspace_policy=workspace_policy,
        skills=skills,
        tools=tools,
        extensions=extensions,
    )
    return AgentPackageVersion(
        package_version_id=package_version_id_for_content(content),
        agent_id=agent_id,
        format_version=format_version,
        behavior_instruction=behavior_instruction,
        model_policy=model_policy,
        runtime_policy=runtime_policy,
        workspace_policy=workspace_policy,
        skills=skills,
        tools=tools,
        extensions=extensions,
        created_at=created_at,
    )


@dataclass(frozen=True)
class LoadedAgentPackage:
    """Generation 内的可执行加载结果，不参与 Package 内容寻址。"""

    version: AgentPackageVersion
    model_clients: Mapping[ModelRole, "Provider"]
    tool_snapshot: ToolSnapshot
    lifecycle_hooks: tuple[Any, ...] = ()
    recall_sources: tuple[Any, ...] = ()
    generation_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "model_clients", MappingProxyType(dict(self.model_clients))
        )
        object.__setattr__(self, "lifecycle_hooks", tuple(self.lifecycle_hooks))
        object.__setattr__(self, "recall_sources", tuple(self.recall_sources))


def canonical_json_bytes(content: Mapping[str, Any]) -> bytes:
    """将 JSON 内容规范化；禁止 NaN、Infinity 和非 JSON 类型。"""
    frozen = freeze_json(content)
    return json.dumps(
        thaw_json(frozen),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def package_version_id_for_content(content: Mapping[str, Any]) -> str:
    return "agentpkg_" + hashlib.sha256(canonical_json_bytes(content)).hexdigest()


def decode_agent_package_content(
    *, package_version_id: str, content: Mapping[str, Any], created_at: datetime
) -> AgentPackageVersion:
    """解码当前格式；旧格式必须经过显式 ``decode_legacy_agent_package``。"""
    if int(content.get("format_version", 0)) != 1:
        raise ValueError("只支持 AgentPackageVersion.format_version=1")
    return _version_from_target_content(content, created_at, package_version_id)


def decode_legacy_agent_package(
    *, content: Mapping[str, Any], created_at: datetime
) -> AgentPackageVersion:
    """将 v2/v3 旧结构转换成新 Package，不暴露旧字段。"""
    schema = int(content.get("schema_version", 0))
    if schema not in {2, 3}:
        raise ValueError("只支持 legacy schema_version=2/3")
    definition = dict(content.get("definition") or {})
    old_model = dict(content.get("model") or {})
    old_runtime = dict(content.get("runtime") or {})
    if schema == 2:
        old_runtime["context_turn_window"] = old_runtime.pop("context_unit_window")
    model = ModelVersion(
        provider=str(old_model["provider"]),
        model=str(old_model["model"]),
        api_base=old_model.get("api_base"),
        temperature=old_model.get("temperature"),
        max_input_tokens=old_model.get("max_input_tokens"),
        max_output_tokens=int(old_model["max_output_tokens"]),
        provider_options=old_model.get("provider_options") or {},
        provider_implementation=ImplementationRef(
            "provider", str(old_model["provider"])
        ),
        required_secret_refs=tuple(
            SecretRef(f"providers.{old_model['provider']}.{name}")
            for name in old_model.get("required_secrets", ())
        ),
    )
    package_content = {
        "format_version": 1,
        "agent_id": str(content["agent_id"]),
        "behavior_instruction": str(content["behavior_instruction"]),
        "model_policy": {
            "primary": _model_dict(model),
            "worker": None,
            "utility": None,
        },
        "runtime_policy": {
            "max_model_steps": int(old_runtime["max_model_steps"]),
            "context_turn_window": int(old_runtime["context_turn_window"]),
            "max_delegation_depth": 3,
        },
        "workspace_policy": {
            "file_scope": str(definition.get("file_access_mode", "workspace"))
        },
        "skills": [_legacy_skill(item) for item in content.get("skills") or ()],
        "tools": [_legacy_tool(item) for item in content.get("tools") or ()],
        "extensions": [],
    }
    return _version_from_target_content(
        package_content, created_at, package_version_id_for_content(package_content)
    )


def _version_from_target_content(
    content: Mapping[str, Any], created_at: datetime, package_version_id: str
) -> AgentPackageVersion:
    if package_version_id != package_version_id_for_content(content):
        raise ValueError("package_version_id 与 canonical Package 内容不一致")
    policy = dict(content["model_policy"])
    return AgentPackageVersion(
        package_version_id=package_version_id,
        agent_id=str(content["agent_id"]),
        format_version=int(content["format_version"]),
        behavior_instruction=str(content["behavior_instruction"]),
        model_policy=_policy_from_dict(policy),
        runtime_policy=AgentRuntimePolicy(**dict(content["runtime_policy"])),
        workspace_policy=WorkspacePolicy(**dict(content["workspace_policy"])),
        skills=tuple(_skill_from_dict(item) for item in content.get("skills") or ()),
        tools=tuple(_tool_from_dict(item) for item in content.get("tools") or ()),
        extensions=tuple(
            _extension_from_dict(item) for item in content.get("extensions") or ()
        ),
        created_at=created_at,
    )


def _content_dict(
    *,
    format_version: int,
    agent_id: str,
    behavior_instruction: str,
    model_policy: ModelPolicy,
    runtime_policy: AgentRuntimePolicy,
    workspace_policy: WorkspacePolicy,
    skills: tuple[SkillVersion, ...],
    tools: tuple[ToolVersion, ...],
    extensions: tuple[ExtensionVersion, ...],
) -> dict[str, Any]:
    return {
        "format_version": format_version,
        "agent_id": agent_id,
        "behavior_instruction": behavior_instruction,
        "model_policy": _model_policy_dict(model_policy),
        "runtime_policy": _runtime_policy_dict(runtime_policy),
        "workspace_policy": {"file_scope": workspace_policy.file_scope},
        "skills": [_skill_dict(item) for item in skills],
        "tools": [_tool_dict(item) for item in tools],
        "extensions": [_extension_dict(item) for item in extensions],
    }


def _ref_dict(ref: ImplementationRef) -> dict[str, Any]:
    return {
        "kind": ref.kind,
        "name": ref.name,
        "version": ref.version,
        "digest": ref.digest,
    }


def _secret_dict(ref: SecretRef) -> dict[str, Any]:
    return {"name": ref.name}


def _model_dict(model: ModelVersion) -> dict[str, Any]:
    return {
        "provider": model.provider,
        "model": model.model,
        "api_base": model.api_base,
        "temperature": model.temperature,
        "max_input_tokens": model.max_input_tokens,
        "max_output_tokens": model.max_output_tokens,
        "provider_options": thaw_json(model.provider_options),
        "provider_implementation": _ref_dict(model.provider_implementation),
        "required_secret_refs": [
            _secret_dict(ref) for ref in model.required_secret_refs
        ],
    }


def _model_policy_dict(policy: ModelPolicy) -> dict[str, Any]:
    return {
        "primary": _model_dict(policy.primary),
        "worker": _model_dict(policy.worker) if policy.worker else None,
        "utility": _model_dict(policy.utility) if policy.utility else None,
    }


def _runtime_policy_dict(policy: AgentRuntimePolicy) -> dict[str, Any]:
    return {
        "max_model_steps": policy.max_model_steps,
        "context_turn_window": policy.context_turn_window,
        "max_delegation_depth": policy.max_delegation_depth,
    }


def _skill_dict(skill: SkillVersion) -> dict[str, Any]:
    return {
        "name": skill.name,
        "version": skill.version,
        "description": skill.description,
        "content": skill.content,
        "required_secret_refs": [
            _secret_dict(ref) for ref in skill.required_secret_refs
        ],
        "allowed_tools": list(skill.allowed_tools),
    }


def _tool_dict(tool: ToolVersion) -> dict[str, Any]:
    return {
        "name": tool.name,
        "source": tool.source.value,
        "implementation_ref": _ref_dict(tool.implementation_ref),
        "version": tool.version,
        "description": tool.description,
        "input_schema": thaw_json(tool.input_schema),
        "output_schema": (
            thaw_json(tool.output_schema) if tool.output_schema is not None else None
        ),
        "replay_policy": tool.replay_policy,
    }


def _extension_dict(extension: ExtensionVersion) -> dict[str, Any]:
    return {
        "extension_id": extension.extension_id,
        "implementation_ref": _ref_dict(extension.implementation_ref),
        "version": extension.version,
        "config": thaw_json(extension.config),
        "required_secret_refs": [
            _secret_dict(ref) for ref in extension.required_secret_refs
        ],
    }


def _ref_from_dict(value: Mapping[str, Any]) -> ImplementationRef:
    return ImplementationRef(**dict(value))


def _secret_refs(values: Any) -> tuple[SecretRef, ...]:
    return tuple(SecretRef(**dict(value)) for value in values or ())


def _model_from_dict(value: Mapping[str, Any]) -> ModelVersion:
    data = dict(value)
    data["provider_implementation"] = _ref_from_dict(data["provider_implementation"])
    data["required_secret_refs"] = _secret_refs(data.get("required_secret_refs"))
    return ModelVersion(**data)


def _policy_from_dict(value: Mapping[str, Any]) -> ModelPolicy:
    return ModelPolicy(
        primary=_model_from_dict(value["primary"]),
        worker=_model_from_dict(value["worker"]) if value.get("worker") else None,
        utility=_model_from_dict(value["utility"]) if value.get("utility") else None,
    )


def _skill_from_dict(value: Mapping[str, Any]) -> SkillVersion:
    data = dict(value)
    data["required_secret_refs"] = _secret_refs(data.get("required_secret_refs"))
    data["allowed_tools"] = tuple(data.get("allowed_tools") or ())
    return SkillVersion(**data)


def _tool_from_dict(value: Mapping[str, Any]) -> ToolVersion:
    data = dict(value)
    data["implementation_ref"] = _ref_from_dict(data["implementation_ref"])
    return ToolVersion(**data)


def _extension_from_dict(value: Mapping[str, Any]) -> ExtensionVersion:
    data = dict(value)
    data["implementation_ref"] = _ref_from_dict(data["implementation_ref"])
    data["required_secret_refs"] = _secret_refs(data.get("required_secret_refs"))
    return ExtensionVersion(**data)


def _legacy_skill(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "name": str(value["name"]),
        "version": str(value.get("version", "")),
        "description": str(value.get("description", "")),
        "content": str(value.get("content", "")),
        "required_secret_refs": [
            {"name": f"environ.{name}"} for name in value.get("required_env", ())
        ],
        "allowed_tools": list(value.get("allowed_tools") or ()),
    }


def _legacy_tool(value: Mapping[str, Any]) -> dict[str, Any]:
    source = str(value.get("source", ToolSource.BUILTIN.value))
    return {
        "name": str(value["name"]),
        "source": source,
        "implementation_ref": {
            "kind": source,
            "name": str(value.get("origin") or value["name"]),
            "version": value.get("version"),
            "digest": None,
        },
        "version": value.get("version"),
        "description": str(value.get("description", "")),
        "input_schema": value.get("input_schema") or {},
        "output_schema": value.get("output_schema"),
        "replay_policy": "never",
    }
