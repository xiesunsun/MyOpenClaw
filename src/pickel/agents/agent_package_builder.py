"""从现有 AppConfig 构建冻结 AgentPackageVersion。"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pickel.agents.agent_package import (
    AgentDefinition,
    AgentPackageVersion,
    AgentRuntimePolicy,
    ExtensionVersion,
    ImplementationRef,
    ModelPolicy,
    ModelVersion,
    SecretRef,
    SkillVersion,
    ToolVersion,
    WorkspacePolicy,
    build_agent_package_version,
)
from pickel.agents.behavior_loader import BehaviorLoader
from pickel.agents.skills import SkillManifest, SkillRegistry
from pickel.config.app_config import AppConfig
from pickel.shared.file_access import FileAccessMode
from pickel.tools.bus import ToolActivation, ToolBus, ToolSnapshot

_SECRET_MARKERS = ("api_key", "token", "secret", "password", "authorization")


@dataclass(frozen=True)
class _SecretScan:
    value: Any
    refs: tuple[SecretRef, ...]


def sanitize_extension_config(
    value: Any, extension_id: str
) -> tuple[dict[str, Any], tuple[SecretRef, ...]]:
    """冻结前移除 Extension 配置中的秘密，并保留逻辑 SecretRef。

    Loader 和 Builder 必须使用同一规则，否则同一配置会产生不同 Package。
    ``ExtensionVersion`` 自身随后还会做深度冻结。
    """
    scan = _strip_secrets(value, f"extensions.{extension_id}")
    clean = scan.value if isinstance(scan.value, dict) else {}
    return clean, scan.refs


def _strip_secrets(value: Any, prefix: str) -> _SecretScan:
    refs: list[SecretRef] = []
    if isinstance(value, Mapping):
        clean: dict[str, Any] = {}
        for key, item in value.items():
            path = f"{prefix}.{key}"
            if any(marker in str(key).lower() for marker in _SECRET_MARKERS):
                refs.append(SecretRef(path))
                continue
            nested = _strip_secrets(item, path)
            clean[str(key)] = nested.value
            refs.extend(nested.refs)
        return _SecretScan(clean, tuple(refs))
    if isinstance(value, list):
        clean_list: list[Any] = []
        for index, item in enumerate(value):
            nested = _strip_secrets(item, f"{prefix}.{index}")
            clean_list.append(nested.value)
            refs.extend(nested.refs)
        return _SecretScan(clean_list, tuple(refs))
    return _SecretScan(value, ())


class AgentPackageBuilder:
    """把配置解析为 Definition，再冻结为 Package。

    当前 AppConfig 只有一套模型选择，因此只填充 ``primary``；缺失的
    ``worker``/``utility`` 明确保持 None，调用方必须选择显式策略。
    """

    def __init__(
        self,
        *,
        app_config: AppConfig,
        tool_bus: ToolBus,
        now: Callable[[], datetime] | None = None,
        extension_versions: Mapping[str, ExtensionVersion] | None = None,
    ) -> None:
        self._app_config = app_config
        self._tool_bus = tool_bus
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._extension_versions = (
            None if extension_versions is None else dict(extension_versions)
        )

    def build_agent_package_version(
        self,
        agent_id: str | None = None,
        *,
        extension_versions: Mapping[str, ExtensionVersion] | None = None,
    ) -> AgentPackageVersion:
        loaded_extensions: Mapping[str, ExtensionVersion] | None = (
            self._extension_versions
            if extension_versions is None
            else dict(extension_versions)
        )
        resolved_id = agent_id or self._app_config.default_agent
        agent_config = self._app_config.get_agent_config(resolved_id)
        self._validate_workspace_path(
            agent_id=resolved_id, workspace_path=agent_config.workspace_path
        )
        skills_path = self.resolve_skills_path(resolved_id)
        primary_config = self._app_config.resolve_model_config(agent_config.llm)
        definition = self._build_definition(
            resolved_id,
            agent_config,
            skills_path,
            primary_config,
            loaded_extensions,
        )
        behavior_instruction = BehaviorLoader.load(agent_config.behavior_path)
        skill_manifests = tuple(SkillRegistry.discover(skills_path))
        tool_snapshot = self._tool_bus.snapshot(
            ToolActivation(allowed=frozenset(agent_config.tools))
        )
        return self._build_version(
            definition=definition,
            behavior_instruction=behavior_instruction,
            skill_manifests=skill_manifests,
            tool_snapshot=tool_snapshot,
            extension_versions=loaded_extensions,
        )

    def resolve_skills_path(self, agent_id: str) -> Path | None:
        agent_config = self._app_config.get_agent_config(agent_id)
        skills_path = self._app_config.resolve_skills_path(agent_id)
        if (
            skills_path is not None
            and skills_path.exists()
            and self._app_config.resolve_file_access_mode(agent_id)
            != FileAccessMode.FULL
            and not self._is_within_workspace(skills_path, agent_config.workspace_path)
        ):
            raise ValueError(
                f"Skills path '{skills_path}' is outside workspace "
                f"'{agent_config.workspace_path}' and requires file_access_mode: full"
            )
        return skills_path

    def _build_definition(
        self,
        agent_id: str,
        agent_config: Any,
        skills_path: Path | None,
        primary_config: Any,
        extension_versions: Mapping[str, ExtensionVersion] | None,
    ) -> AgentDefinition:
        runtime = AgentRuntimePolicy(
            max_model_steps=self._app_config.react_max_steps,
            context_turn_window=self._app_config.context_cli_turn_window,
            max_delegation_depth=3,
        )
        return AgentDefinition(
            agent_id=agent_id,
            default_workspace_path=agent_config.workspace_path,
            workspace_policy=WorkspacePolicy(
                self._app_config.resolve_file_access_mode(agent_id).value
            ),
            behavior_path=agent_config.behavior_path,
            skills_path=skills_path,
            allowed_tools=tuple(agent_config.tools),
            extension_ids=self._extension_ids(
                agent_config.extensions, extension_versions
            ),
            model_policy=ModelPolicy(primary=self._model_version(primary_config)),
            runtime_policy=runtime,
        )

    def _build_version(
        self,
        *,
        definition: AgentDefinition,
        behavior_instruction: str,
        skill_manifests: tuple[SkillManifest, ...],
        tool_snapshot: ToolSnapshot,
        extension_versions: Mapping[str, ExtensionVersion] | None,
    ) -> AgentPackageVersion:
        skills = tuple(self._skill_version(manifest) for manifest in skill_manifests)
        tools = tuple(self._tool_version(entry) for entry in tool_snapshot.entries)
        extensions = tuple(
            self._extension_version(extension_id, extension_versions)
            for extension_id in definition.extension_ids
        )
        return build_agent_package_version(
            agent_id=definition.agent_id,
            format_version=1,
            behavior_instruction=behavior_instruction,
            model_policy=definition.model_policy,
            runtime_policy=definition.runtime_policy,
            workspace_policy=definition.workspace_policy,
            skills=skills,
            tools=tools,
            extensions=extensions,
            created_at=self._now(),
        )

    def _model_version(self, config: Any) -> ModelVersion:
        scan = self._strip_secrets(
            config.provider_options, f"providers.{config.provider}.options"
        )
        refs = list(scan.refs)
        if config.api_key:
            refs.append(SecretRef(f"providers.{config.provider}.api_key"))
        return ModelVersion(
            provider=config.provider,
            model=config.model,
            api_base=config.api_base,
            temperature=config.temperature,
            max_input_tokens=config.max_input_tokens,
            max_output_tokens=config.max_output_tokens,
            provider_options=scan.value,
            provider_implementation=ImplementationRef("provider", config.provider),
            required_secret_refs=tuple(refs),
        )

    def _tool_version(self, entry: Any) -> ToolVersion:
        return ToolVersion(
            name=entry.name,
            source=entry.source,
            implementation_ref=ImplementationRef(
                entry.source.value, entry.origin or entry.name, entry.version
            ),
            version=entry.version,
            description=entry.tool.spec.description,
            input_schema=entry.tool.spec.input_schema,
            output_schema=entry.tool.spec.output_schema,
            replay_policy="never",
        )

    @staticmethod
    def _skill_version(manifest: SkillManifest) -> SkillVersion:
        return SkillVersion(
            name=manifest.name,
            description=manifest.description,
            version=manifest.version,
            content=manifest.skill_file.read_text(encoding="utf-8"),
            required_secret_refs=tuple(
                SecretRef(f"environ.{name}") for name in manifest.required_env
            ),
            allowed_tools=manifest.allowed_tools,
        )

    def _extension_version(
        self,
        extension_id: str,
        loaded_extensions: Mapping[str, ExtensionVersion] | None,
    ) -> ExtensionVersion:
        if loaded_extensions is not None:
            version = loaded_extensions.get(extension_id)
            if version is None:
                raise ValueError(
                    f"Extension '{extension_id}' 未在当前 Generation 精确装载"
                )
            if version.extension_id != extension_id:
                raise ValueError(
                    f"ExtensionVersion.extension_id 与 '{extension_id}' 不一致"
                )
            return version
        raw = self._app_config.extensions.get(extension_id) or {}
        scan = self._strip_secrets(raw, f"extensions.{extension_id}")
        version = raw.get("version") if isinstance(raw.get("version"), str) else None
        return ExtensionVersion(
            extension_id=extension_id,
            implementation_ref=ImplementationRef("extension", extension_id, version),
            version=version,
            config=scan.value,
            required_secret_refs=scan.refs,
        )

    def _extension_ids(
        self,
        values: list[str],
        loaded_extensions: Mapping[str, ExtensionVersion] | None,
    ) -> tuple[str, ...]:
        if "*" in values:
            if loaded_extensions is not None:
                return tuple(sorted(loaded_extensions))
            return tuple(sorted(self._app_config.extensions))
        return tuple(values)

    @classmethod
    def _strip_secrets(cls, value: Any, prefix: str) -> _SecretScan:
        return _strip_secrets(value, prefix)

    @staticmethod
    def _validate_workspace_path(*, agent_id: str, workspace_path: Path) -> None:
        if not workspace_path.exists():
            raise ValueError(
                f"Agent '{agent_id}' workspace_path 不存在: {workspace_path}"
            )
        if not workspace_path.is_dir():
            raise ValueError(
                f"Agent '{agent_id}' workspace_path 不是目录: {workspace_path}"
            )

    @staticmethod
    def _is_within_workspace(path: Path, workspace_path: Path) -> bool:
        try:
            path.resolve().relative_to(workspace_path.resolve())
        except ValueError:
            return False
        return True
