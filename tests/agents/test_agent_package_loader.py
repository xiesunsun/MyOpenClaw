from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from pickel.agents.agent_package import (
    ExtensionVersion,
    ImplementationRef,
    build_agent_package_version,
)
from pickel.agents.agent_package_loader import PackageLoadError
from pickel.app.boot import Boot
from pickel.persistence.in_memory_runtime_store import InMemoryRuntimeStore
from pickel.config.app_config import ModelSelection
from pickel.extensions_host.host import ExtensionHost
from pickel.extensions_host.registry import ExtensionRegistry
from pickel.tools.bus import ToolSource
from tests.agents.test_agent_package_builder import _EchoTool
from tests.agents.test_agent_package_builder import _config, _tool_bus


def _extension_registry(version: ExtensionVersion, bus) -> ExtensionRegistry:
    registry = ExtensionRegistry()
    host = ExtensionHost(
        name=version.extension_id,
        config_section={},
        tool_bus=bus,
        registry=registry,
        defer_publish=True,
        extension_version=version,
    )
    host.add_hook_handler(lambda scope: f"hook-{scope.agent_id}")
    host.add_recall_source(lambda scope: f"recall-{scope.agent_id}")
    host.publish()
    return registry


def test_loads_the_stored_version_instead_of_rebuilding_current_config(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    config.agents["Pickle"].extensions = []
    boot = Boot(config, tool_bus=_tool_bus())
    store = InMemoryRuntimeStore()

    old = boot.resolve_loaded_agent_package()
    store.insert_agent_package_version(old.version)
    config.providers["anthropic"].models["claude-current"] = config.providers[
        "anthropic"
    ].models["claude-test"]
    config.agents["Pickle"].llm = ModelSelection(
        provider="anthropic", model="claude-current"
    )

    restored = boot.load_agent_package(
        old.version.package_version_id,
        store=store,
        expected_agent_id="Pickle",
    )

    assert restored.version.package_version_id == old.version.package_version_id
    assert restored.model_clients["primary"].model == "claude-test"


def test_missing_package_has_stable_failure_code(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.agents["Pickle"].extensions = []
    boot = Boot(config, tool_bus=_tool_bus())

    with pytest.raises(PackageLoadError) as caught:
        boot.load_agent_package("agentpkg_" + "0" * 64, store=InMemoryRuntimeStore())

    assert caught.value.code == "package_version_missing"


def test_tool_implementation_mismatch_does_not_fallback_to_current_tool(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    config.agents["Pickle"].extensions = []
    boot = Boot(config, tool_bus=_tool_bus())
    store = InMemoryRuntimeStore()
    version = boot.resolve_loaded_agent_package().version
    store.insert_agent_package_version(version)
    boot.tool_bus.unregister("echo")
    boot.tool_bus.register(_EchoTool(), source=ToolSource.BUILTIN, version="changed")

    with pytest.raises(PackageLoadError) as caught:
        boot.load_agent_package(version.package_version_id, store=store)

    assert caught.value.code == "tool_unavailable"


def test_provider_version_that_cannot_be_verified_is_rejected(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.agents["Pickle"].extensions = []
    boot = Boot(config, tool_bus=_tool_bus())
    store = InMemoryRuntimeStore()
    current = boot.resolve_loaded_agent_package().version
    primary = replace(
        current.model_policy.primary,
        provider_implementation=ImplementationRef(
            "provider", "anthropic", version="unavailable-version"
        ),
    )
    version = build_agent_package_version(
        agent_id=current.agent_id,
        format_version=current.format_version,
        behavior_instruction=current.behavior_instruction,
        model_policy=replace(current.model_policy, primary=primary),
        runtime_policy=current.runtime_policy,
        workspace_policy=current.workspace_policy,
        skills=current.skills,
        tools=current.tools,
        extensions=current.extensions,
        created_at=current.created_at,
    )
    store.insert_agent_package_version(version)

    with pytest.raises(PackageLoadError) as caught:
        boot.load_agent_package(version.package_version_id, store=store)

    assert caught.value.code == "provider_implementation_unavailable"


def test_extension_contributions_are_loaded_from_the_exact_registered_version(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    bus = _tool_bus()
    extension = ExtensionVersion(
        extension_id="openviking",
        implementation_ref=ImplementationRef(
            "extension", "openviking", version="1", digest="a" * 64
        ),
        version="1",
        config={},
    )
    boot = Boot(
        config,
        tool_bus=bus,
        extensions=_extension_registry(extension, bus),
    )
    store = InMemoryRuntimeStore()
    current = boot.resolve_loaded_agent_package()
    store.insert_agent_package_version(current.version)

    restored = boot.load_agent_package(
        current.version.package_version_id,
        store=store,
        expected_agent_id="Pickle",
    )

    assert restored.version.extensions == (extension,)
    assert restored.lifecycle_hooks == ("hook-Pickle",)
    assert restored.recall_sources == ("recall-Pickle",)


def test_extension_version_mismatch_never_uses_current_same_name_extension(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    old_bus = _tool_bus()
    old_extension = ExtensionVersion(
        extension_id="openviking",
        implementation_ref=ImplementationRef(
            "extension", "openviking", version="1", digest="a" * 64
        ),
        version="1",
        config={},
    )
    old_boot = Boot(
        config,
        tool_bus=old_bus,
        extensions=_extension_registry(old_extension, old_bus),
    )
    package = old_boot.resolve_loaded_agent_package().version
    store = InMemoryRuntimeStore()
    store.insert_agent_package_version(package)

    current_bus = _tool_bus()
    current_extension = replace(
        old_extension,
        implementation_ref=ImplementationRef(
            "extension", "openviking", version="2", digest="b" * 64
        ),
        version="2",
    )
    current_boot = Boot(
        config,
        tool_bus=current_bus,
        extensions=_extension_registry(current_extension, current_bus),
    )

    with pytest.raises(PackageLoadError) as caught:
        current_boot.load_agent_package(
            package.package_version_id,
            store=store,
            expected_agent_id="Pickle",
        )

    assert caught.value.code == "extension_unavailable"
