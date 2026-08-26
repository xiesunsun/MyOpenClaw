from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from pickel.agents.agent_package import (
    ExtensionVersion,
    ImplementationRef,
    build_agent_package_version,
)
from pickel.agents.agent_package_loader import AgentPackageLoader, PackageLoadError
from pickel.app.boot import Boot
from pickel.artifacts.artifact_service import ArtifactService
from pickel.artifacts.in_memory_blob_store import InMemoryBlobStore
from pickel.persistence.in_memory_runtime_store import InMemoryRuntimeStore
from pickel.config.app_config import ModelSelection
from pickel.extensions_host.host import ExtensionHost
from pickel.extensions_host.registry import ExtensionRegistry
from pickel.providers.openai import OpenAIProvider
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


def _artifact_service(store: InMemoryRuntimeStore) -> ArtifactService:
    return ArtifactService(
        artifact_store=store,
        blob_store=InMemoryBlobStore(),
    )


def test_loads_the_stored_version_instead_of_rebuilding_current_config(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    config.agents["Pickle"].extensions = []
    boot = Boot(config, tool_bus=_tool_bus())
    store = InMemoryRuntimeStore()

    old = boot.resolve_loaded_agent_package(artifact_service=_artifact_service(store))
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
        artifact_service=_artifact_service(store),
        expected_agent_id="Pickle",
    )

    assert restored.version.package_version_id == old.version.package_version_id
    assert restored.model_clients["primary"].model == "claude-test"


def test_missing_package_has_stable_failure_code(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.agents["Pickle"].extensions = []
    boot = Boot(config, tool_bus=_tool_bus())

    store = InMemoryRuntimeStore()
    with pytest.raises(PackageLoadError) as caught:
        boot.load_agent_package(
            "agentpkg_" + "0" * 64,
            store=store,
            artifact_service=_artifact_service(store),
        )

    assert caught.value.code == "package_version_missing"


def test_new_package_with_unsupported_provider_has_stable_failure_code(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    config.agents["Pickle"].extensions = []
    config.providers["google/gemini"] = type(config.providers["anthropic"])(
        models={"gemini-test": config.providers["anthropic"].models["claude-test"]}
    )
    config.agents["Pickle"].llm = ModelSelection(
        provider="google/gemini", model="gemini-test"
    )
    boot = Boot(config, tool_bus=_tool_bus())

    store = InMemoryRuntimeStore()
    with pytest.raises(PackageLoadError) as caught:
        boot.resolve_loaded_agent_package(artifact_service=_artifact_service(store))

    assert caught.value.code == "provider_unsupported"
    assert caught.value.package_version_id.startswith("agentpkg_")


def test_new_and_frozen_packages_load_openai_responses_provider(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.agents["Pickle"].extensions = []
    config.providers["openai"] = type(config.providers["anthropic"])(
        models={"gpt-5.6-luna": config.providers["anthropic"].models["claude-test"]}
    )
    config.agents["Pickle"].llm = ModelSelection(
        provider="openai", model="gpt-5.6-luna"
    )
    boot = Boot(config, tool_bus=_tool_bus())
    store = InMemoryRuntimeStore()

    current = boot.resolve_loaded_agent_package(
        artifact_service=_artifact_service(store)
    )
    store.insert_agent_package_version(current.version)
    restored = boot.load_agent_package(
        current.version.package_version_id,
        store=store,
        artifact_service=_artifact_service(store),
    )

    assert isinstance(current.model_clients["primary"], OpenAIProvider)
    assert isinstance(restored.model_clients["primary"], OpenAIProvider)
    assert current.model_clients["primary"].model == "gpt-5.6-luna"
    assert restored.version.package_version_id == current.version.package_version_id


def test_frozen_package_with_unsupported_provider_has_stable_failure_code(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    config.agents["Pickle"].extensions = []
    boot = Boot(config, tool_bus=_tool_bus())
    store = InMemoryRuntimeStore()
    current = boot.resolve_loaded_agent_package(
        artifact_service=_artifact_service(store)
    ).version
    primary = replace(
        current.model_policy.primary,
        provider="google/gemini",
        provider_implementation=ImplementationRef("provider", "google/gemini"),
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
        boot.load_agent_package(
            version.package_version_id,
            store=store,
            artifact_service=_artifact_service(store),
        )

    assert caught.value.code == "provider_unsupported"
    assert caught.value.package_version_id == version.package_version_id


def test_provider_loader_does_not_swallow_precise_package_load_error(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    config.agents["Pickle"].extensions = []
    boot = Boot(config, tool_bus=_tool_bus())
    store = InMemoryRuntimeStore()
    version = boot.resolve_loaded_agent_package(
        artifact_service=_artifact_service(store)
    ).version
    store.insert_agent_package_version(version)
    precise = PackageLoadError(
        "provider_unsupported", version.package_version_id, "测试错误"
    )

    def provider_loader(_model):
        raise precise

    loader = AgentPackageLoader(
        store,
        boot.tool_bus,
        provider_loader=provider_loader,
    )

    with pytest.raises(PackageLoadError) as caught:
        loader.load(version.package_version_id)

    assert caught.value is precise
    assert caught.value.code == "provider_unsupported"


def test_tool_implementation_mismatch_does_not_fallback_to_current_tool(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    config.agents["Pickle"].extensions = []
    boot = Boot(config, tool_bus=_tool_bus())
    store = InMemoryRuntimeStore()
    version = boot.resolve_loaded_agent_package(
        artifact_service=_artifact_service(store)
    ).version
    store.insert_agent_package_version(version)
    boot.tool_bus.unregister("echo")
    boot.tool_bus.register(_EchoTool(), source=ToolSource.BUILTIN, version="changed")

    with pytest.raises(PackageLoadError) as caught:
        boot.load_agent_package(
            version.package_version_id,
            store=store,
            artifact_service=_artifact_service(store),
        )

    assert caught.value.code == "tool_unavailable"


def test_provider_version_that_cannot_be_verified_is_rejected(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.agents["Pickle"].extensions = []
    boot = Boot(config, tool_bus=_tool_bus())
    store = InMemoryRuntimeStore()
    current = boot.resolve_loaded_agent_package(
        artifact_service=_artifact_service(store)
    ).version
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
        boot.load_agent_package(
            version.package_version_id,
            store=store,
            artifact_service=_artifact_service(store),
        )

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
    current = boot.resolve_loaded_agent_package(
        artifact_service=_artifact_service(store)
    )
    store.insert_agent_package_version(current.version)

    restored = boot.load_agent_package(
        current.version.package_version_id,
        store=store,
        artifact_service=_artifact_service(store),
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
    store = InMemoryRuntimeStore()
    package = old_boot.resolve_loaded_agent_package(
        artifact_service=_artifact_service(store)
    ).version
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
            artifact_service=_artifact_service(store),
            expected_agent_id="Pickle",
        )

    assert caught.value.code == "extension_unavailable"
