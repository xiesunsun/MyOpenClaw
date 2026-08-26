"""按持久化 Package Version 精确装载运行时实现。

Loader 的输入必须是 Operation 已绑定的 ``package_version_id``。它不会调用
Builder，也不会从当前 AppConfig 重新生成 Package；当前进程没有对应实现时，
返回稳定的 ``PackageLoadError``，由恢复流程把 Operation 标记为失败。
"""

from __future__ import annotations

from typing import Any, Protocol

from pickel.agents.agent_package import (
    AgentPackageVersion,
    ExtensionVersion,
    LoadedAgentPackage,
    ModelVersion,
    ToolVersion,
)
from pickel.agents.agent_package_store import AgentPackageVersionStore
from pickel.tools.bus import ToolBus, ToolEntry, ToolSnapshot


class PackageLoadError(RuntimeError):
    """Package 快照或其实现无法在当前 Generation 中重建。"""

    def __init__(self, code: str, package_version_id: str, detail: str) -> None:
        self.code = code
        self.package_version_id = package_version_id
        self.detail = detail
        super().__init__(f"{code}: package={package_version_id}: {detail}")


class ProviderLoader(Protocol):
    def __call__(self, model: ModelVersion) -> Any | None: ...


class ExtensionLoader(Protocol):
    def __call__(
        self, extension: ExtensionVersion
    ) -> tuple[tuple[Any, ...], tuple[Any, ...]]: ...


class AgentPackageLoader:
    """从 Store 的冻结内容重建一个 LoadedAgentPackage。

    Tool 默认通过 ``ToolBus`` 按快照中的 ImplementationRef 校验；Provider 和
    Extension 通过显式回调注入。没有回调意味着该实现不可恢复，而不是改用当前
    配置中的同名实现。
    """

    def __init__(
        self,
        store: AgentPackageVersionStore,
        tool_bus: ToolBus,
        *,
        provider_loader: ProviderLoader,
        extension_loader: ExtensionLoader | None = None,
    ) -> None:
        self._store = store
        self._tool_bus = tool_bus
        self._provider_loader = provider_loader
        self._extension_loader = extension_loader

    def load(
        self,
        package_version_id: str,
        *,
        expected_agent_id: str | None = None,
    ) -> LoadedAgentPackage:
        version = self._store.load_agent_package_version(package_version_id)
        if version is None:
            raise PackageLoadError(
                "package_version_missing", package_version_id, "Store 中不存在冻结版本"
            )
        if version.package_version_id != package_version_id:
            raise PackageLoadError(
                "package_version_identity_mismatch",
                package_version_id,
                "Store 返回的 Package 身份不一致",
            )
        if expected_agent_id is not None and version.agent_id != expected_agent_id:
            raise PackageLoadError(
                "package_agent_mismatch",
                package_version_id,
                f"需要 agent={expected_agent_id}，实际为 {version.agent_id}",
            )

        model_clients: dict[str, Any] = {}
        for role, model in (
            ("primary", version.model_policy.primary),
            ("worker", version.model_policy.worker),
            ("utility", version.model_policy.utility),
        ):
            if model is None:
                continue
            implementation = model.provider_implementation
            if (
                implementation.kind != "provider"
                or implementation.name != model.wire_protocol
            ):
                raise PackageLoadError(
                    "provider_implementation_mismatch",
                    package_version_id,
                    f"{role} Provider ImplementationRef 与 ModelVersion 不一致",
                )
            if implementation.version is not None or implementation.digest is not None:
                raise PackageLoadError(
                    "provider_implementation_unavailable",
                    package_version_id,
                    "当前 Generation 无法校验 Provider 版本或 digest: "
                    f"{implementation.name}",
                )
            try:
                client = self._provider_loader(model)
            except PackageLoadError:
                # Provider loader 已经给出可恢复流程依赖的精确原因码；不能
                # 把 provider_unsupported 等确定错误降级为 provider_unavailable。
                raise
            except Exception as exc:
                raise PackageLoadError(
                    "provider_unavailable",
                    package_version_id,
                    f"{role} provider 装载失败: {exc}",
                ) from exc
            if client is None:
                raise PackageLoadError(
                    "provider_unavailable",
                    package_version_id,
                    f"{role} provider implementation 不可用: {model.provider_implementation.name}",
                )
            model_clients[role] = client

        entries: list[ToolEntry] = []
        for tool in version.tools:
            entry = self._tool_bus_entry(tool)
            if entry is None:
                raise PackageLoadError(
                    "tool_unavailable",
                    package_version_id,
                    f"Tool implementation 不可用或版本不匹配: {tool.name}",
                )
            entries.append(entry)

        hooks: list[Any] = []
        recalls: list[Any] = []
        for extension in version.extensions:
            if self._extension_loader is None:
                raise PackageLoadError(
                    "extension_unavailable",
                    package_version_id,
                    f"Extension implementation 未注册: {extension.extension_id}",
                )
            try:
                extension_hooks, extension_recalls = self._extension_loader(extension)
            except Exception as exc:
                raise PackageLoadError(
                    "extension_unavailable",
                    package_version_id,
                    f"Extension 装载失败: {extension.extension_id}: {exc}",
                ) from exc
            hooks.extend(extension_hooks)
            recalls.extend(extension_recalls)

        return LoadedAgentPackage(
            version=version,
            model_clients=model_clients,
            tool_snapshot=ToolSnapshot(entries=tuple(entries)),
            lifecycle_hooks=tuple(hooks),
            recall_sources=tuple(recalls),
        )

    def _tool_bus_entry(self, version: ToolVersion) -> ToolEntry | None:
        try:
            entry = self._tool_bus.get(version.name)
        except KeyError:
            return None
        ref = version.implementation_ref
        expected_origin = None if entry.source.value == "builtin" else ref.name
        if (
            entry.name != version.name
            or entry.source != version.source
            or entry.version != version.version
            or entry.origin != expected_origin
            or ref.kind != entry.source.value
        ):
            return None
        if ref.version is not None and entry.version != ref.version:
            return None
        if ref.digest is not None:
            # 当前 ToolBus 没有 digest 元数据；不能假设同名实现相同。
            return None
        return entry


__all__ = ["AgentPackageLoader", "PackageLoadError"]
