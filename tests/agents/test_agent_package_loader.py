from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from pickel.agents.agent_package import (
    ExtensionVersion,
    ImplementationRef,
    ToolVersion,
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
from pickel.providers.openai import OpenAIResponsesProvider
from pickel.providers.openai_chat_completions import OpenAIChatCompletionsProvider
from pickel.providers.anthropic import AnthropicMessagesProvider
from pickel.shared.storage_errors import StorageIntegrityError
from pickel.tools.bus import ToolSource, ToolActivation
from pickel.tools.cancel_delegation import cancel_delegation
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
    config.agents["Pickle"].models.primary = ModelSelection(
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


def test_legacy_cancel_tool_is_hidden_from_new_snapshot_but_loadable(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    config.agents["Pickle"].extensions = []
    boot = Boot(config, tool_bus=_tool_bus())
    store = InMemoryRuntimeStore()
    current = boot.resolve_loaded_agent_package(
        artifact_service=_artifact_service(store)
    ).version
    legacy_tool = ToolVersion(
        name="cancel_delegation",
        source=ToolSource.BUILTIN,
        implementation_ref=ImplementationRef("builtin", "cancel_delegation"),
        version=None,
        description=cancel_delegation.spec.description,
        input_schema=cancel_delegation.spec.input_schema,
        output_schema=cancel_delegation.spec.output_schema,
        replay_policy="safe",
    )
    legacy = build_agent_package_version(
        agent_id=current.agent_id,
        # 固定为历史格式；新 Package 升级到 format 3 后仍需覆盖隐藏兼容装载。
        format_version=2,
        behavior_instruction=current.behavior_instruction,
        model_policy=current.model_policy,
        runtime_policy=current.runtime_policy,
        workspace_policy=current.workspace_policy,
        skills=current.skills,
        tools=(legacy_tool,),
        extensions=current.extensions,
        created_at=current.created_at,
    )
    store.insert_agent_package_version(legacy)

    assert "cancel_delegation" not in {
        entry.name
        for entry in _tool_bus()
        .snapshot(ToolActivation(allowed=frozenset({"cancel_delegation"})))
        .entries
    }
    loaded = AgentPackageLoader(
        store, _tool_bus(), provider_loader=lambda model: object()
    ).load(legacy.package_version_id)
    assert loaded.tool_snapshot.names == ("cancel_delegation",)


def test_legacy_wait_tool_is_hidden_from_new_snapshot_but_loadable(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    config.agents["Pickle"].extensions = []
    boot = Boot(config, tool_bus=_tool_bus())
    store = InMemoryRuntimeStore()
    current = boot.resolve_loaded_agent_package(
        artifact_service=_artifact_service(store)
    ).version
    legacy_tool = ToolVersion(
        name="wait_delegation",
        source=ToolSource.BUILTIN,
        implementation_ref=ImplementationRef("builtin", "wait_delegation"),
        version=None,
        description=(
            "Wait for a durable direct child agent for a bounded time. Returns its "
            "persisted final assistant response when terminal; timeout does not cancel it."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "child_session_id": {"type": "string"},
                "timeout_seconds": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 600,
                },
            },
            "required": ["child_session_id", "timeout_seconds"],
            "additionalProperties": False,
        },
        output_schema={
            "type": "object",
            "properties": {
                "timed_out": {"type": "boolean"},
                "agent": {"type": "object"},
                "assistant_message": {"type": ["object", "null"]},
            },
            "required": ["timed_out", "agent", "assistant_message"],
            "additionalProperties": False,
        },
        replay_policy="safe",
    )
    legacy = build_agent_package_version(
        agent_id=current.agent_id,
        # 固定为历史格式；新 Package 不应重新公开 wait_delegation。
        format_version=2,
        behavior_instruction=current.behavior_instruction,
        model_policy=current.model_policy,
        runtime_policy=current.runtime_policy,
        workspace_policy=current.workspace_policy,
        skills=current.skills,
        tools=(legacy_tool,),
        extensions=current.extensions,
        created_at=current.created_at,
    )
    store.insert_agent_package_version(legacy)

    assert "wait_delegation" not in {
        entry.name
        for entry in _tool_bus()
        .snapshot(ToolActivation(allowed=frozenset({"wait_delegation"})))
        .entries
    }
    loaded = AgentPackageLoader(
        store, _tool_bus(), provider_loader=lambda model: object()
    ).load(legacy.package_version_id)
    assert loaded.tool_snapshot.names == ("wait_delegation",)
    loaded_wait = loaded.tool_snapshot.entries[0].tool
    assert set(loaded_wait.spec.output_schema["properties"]) == {
        "timed_out",
        "agent",
        "assistant_message",
    }


def test_loader_rejects_tool_without_output_schema(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.agents["Pickle"].extensions = []
    boot = Boot(config, tool_bus=_tool_bus())
    store = InMemoryRuntimeStore()
    current = boot.resolve_loaded_agent_package(
        artifact_service=_artifact_service(store)
    ).version
    malformed = SimpleNamespace(
        package_version_id=current.package_version_id,
        agent_id=current.agent_id,
        model_policy=current.model_policy,
        tools=(SimpleNamespace(name="echo", output_schema=None),),
        extensions=current.extensions,
    )

    class _MalformedStore:
        def load_agent_package_version(self, _package_version_id):
            return malformed

    with pytest.raises(PackageLoadError, match="output_schema") as caught:
        AgentPackageLoader(
            _MalformedStore(), _tool_bus(), provider_loader=lambda _model: object()
        ).load(current.package_version_id)

    assert caught.value.code == "package_invalid"


def test_storage_integrity_error_maps_to_stable_package_load_error() -> None:
    """Store 端内容损坏必须转成稳定 PackageLoadError，供恢复流程隔离收敛。"""

    class _CorruptStore:
        def load_agent_package_version(self, _package_version_id):
            raise StorageIntegrityError("AgentPackageVersion 内容损坏")

    with pytest.raises(PackageLoadError) as caught:
        AgentPackageLoader(
            _CorruptStore(), _tool_bus(), provider_loader=lambda _model: object()
        ).load("agentpkg_" + "0" * 64)

    assert caught.value.code == "package_integrity_violation"


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
    with pytest.raises(PackageLoadError) as caught:
        Boot._require_supported_wire_protocol(
            "future-wire", package_version_id="agentpkg_test"
        )

    assert caught.value.code == "provider_unsupported"
    assert caught.value.package_version_id == "agentpkg_test"


def test_new_and_frozen_packages_load_openai_responses_provider(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.agents["Pickle"].extensions = []
    config.providers["openai"] = type(config.providers["anthropic"])(
        models={"gpt-5.6-luna": config.providers["anthropic"].models["claude-test"]}
    )
    config.agents["Pickle"].models.primary = ModelSelection(
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

    assert isinstance(current.model_clients["primary"], OpenAIResponsesProvider)
    assert isinstance(restored.model_clients["primary"], OpenAIResponsesProvider)
    assert current.model_clients["primary"].model == "gpt-5.6-luna"
    assert restored.version.package_version_id == current.version.package_version_id


def test_opencode_go_dispatches_each_model_role_by_frozen_wire_protocol(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    config.agents["Pickle"].extensions = []
    catalog_type = type(config.providers["anthropic"])
    model_type = type(config.providers["anthropic"].models["claude-test"])
    config.providers["opencode-go"] = catalog_type(
        models={
            "gpt-5.6-luna": model_type(
                wire_protocol="openai-responses", max_output_tokens=1024
            ),
            "kimi-k3": model_type(
                wire_protocol="openai-chat-completions", max_output_tokens=1024
            ),
            "minimax-m3": model_type(
                wire_protocol="anthropic-messages", max_output_tokens=1024
            ),
        }
    )
    config.auth_providers["opencode-go"] = {
        "api_key": "go-secret",
        "api_base": "https://opencode.ai/zen/go/v1",
    }
    config.agents["Pickle"].models.primary = ModelSelection(
        provider="opencode-go", model="gpt-5.6-luna"
    )
    config.agents["Pickle"].models.worker = ModelSelection(
        provider="opencode-go", model="kimi-k3"
    )
    config.agents["Pickle"].models.utility = ModelSelection(
        provider="opencode-go", model="minimax-m3"
    )
    boot = Boot(config, tool_bus=_tool_bus())
    store = InMemoryRuntimeStore()

    loaded = boot.resolve_loaded_agent_package(
        artifact_service=_artifact_service(store)
    )
    store.insert_agent_package_version(loaded.version)
    restored = boot.load_agent_package(
        loaded.version.package_version_id,
        store=store,
        artifact_service=_artifact_service(store),
    )

    assert isinstance(restored.model_clients["primary"], OpenAIResponsesProvider)
    assert isinstance(restored.model_clients["worker"], OpenAIChatCompletionsProvider)
    assert isinstance(restored.model_clients["utility"], AnthropicMessagesProvider)
    assert all(
        client.provider_name == "opencode-go"
        for client in restored.model_clients.values()
    )
    assert {
        model.wire_protocol
        for model in (
            restored.version.model_policy.primary,
            restored.version.model_policy.worker,
            restored.version.model_policy.utility,
        )
        if model is not None
    } == {
        "openai-responses",
        "openai-chat-completions",
        "anthropic-messages",
    }


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
        wire_protocol="future-wire",
        provider_implementation=ImplementationRef("provider", "future-wire"),
    )
    version = build_agent_package_version(
        agent_id=current.agent_id,
        format_version=2,
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
            "provider", "anthropic-messages", version="unavailable-version"
        ),
    )
    version = build_agent_package_version(
        agent_id=current.agent_id,
        format_version=2,
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
