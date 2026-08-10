"""AgentRun 接受时冻结的进程内实现绑定。"""

from __future__ import annotations

from dataclasses import dataclass, field
from numbers import Real
from pathlib import Path

from pickel.agents.agent_package import AgentPackageVersion
from pickel.artifacts.artifact_service import ArtifactService
from pickel.context.recall import Recall
from pickel.hooks.lifecycle import LifecycleHooks, NoopLifecycleHooks
from pickel.providers.base import Provider
from pickel.tools.bus import ToolSnapshot
from pickel.tools.services import ToolServices


class RuntimeBindingError(ValueError):
    pass


@dataclass(frozen=True)
class RuntimeBindings:
    """Package Version 对应的 Provider、工具、Hook 和宿主服务只读绑定。"""

    agent_package_version: AgentPackageVersion
    provider: Provider
    tool_snapshot: ToolSnapshot
    lifecycle_hooks: LifecycleHooks = field(default_factory=NoopLifecycleHooks)
    recall_sources: tuple[Recall, ...] = ()
    tool_services: ToolServices = field(default_factory=ToolServices)
    artifact_service: ArtifactService | None = None

    DEFAULT_PROVIDER_TIMEOUT_SECONDS = 600.0

    def __post_init__(self) -> None:
        version = self.agent_package_version
        if version.model.provider != "anthropic":
            raise RuntimeBindingError(
                "新 Agent Runtime 当前只接受 Anthropic Provider: "
                f"{version.model.provider}"
            )
        if (
            version.definition.provider != version.model.provider
            or version.definition.model != version.model.model
        ):
            raise RuntimeBindingError("AgentDefinition 与 AgentModelVersion 选择不一致")
        expected_tools = tuple(
            (
                tool.name,
                tool.source,
                tool.version,
                tool.origin,
                tool.description,
                tool.input_schema,
                tool.output_schema,
            )
            for tool in version.tools
        )
        actual_tools = tuple(
            (
                entry.name,
                entry.source.value,
                entry.version,
                entry.origin,
                entry.tool.spec.description,
                entry.tool.spec.input_schema,
                entry.tool.spec.output_schema,
            )
            for entry in self.tool_snapshot.entries
        )
        if actual_tools != expected_tools:
            raise RuntimeBindingError(
                "ToolSnapshot 与 AgentPackageVersion.tools 不一致"
            )
        provider_artifact_service = getattr(
            self.provider,
            "artifact_service",
            None,
        )
        if provider_artifact_service is not self.artifact_service:
            raise RuntimeBindingError(
                "Provider 与 RuntimeBindings 必须共享同一 ArtifactService"
            )

    @property
    def agent_id(self) -> str:
        return self.agent_package_version.agent_id

    @property
    def workspace_path(self) -> Path:
        return Path(self.agent_package_version.definition.workspace_path)

    @property
    def provider_timeout_seconds(self) -> float:
        value = self.agent_package_version.model.provider_options.get("timeout_seconds")
        if not isinstance(value, Real) or isinstance(value, bool):
            return self.DEFAULT_PROVIDER_TIMEOUT_SECONDS
        timeout = float(value)
        if timeout <= 0:
            return self.DEFAULT_PROVIDER_TIMEOUT_SECONDS
        return timeout
