from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from pickel.agents.agent_package_builder import AgentPackageBuilder
from pickel.artifacts.artifact_service import ArtifactService
from pickel.artifacts.in_memory_blob_store import InMemoryBlobStore
from pickel.agents.agent_package import ExtensionVersion, ImplementationRef
from pickel.app.boot import Boot
from pickel.config.app_config import AppConfig
from pickel.shared.model_config import ModelSelection
from pickel.persistence.in_memory_runtime_store import InMemoryRuntimeStore
from pickel.persistence.sqlite_runtime_store import SQLiteRuntimeStore
from pickel.tools.base import (
    BaseTool,
    ToolExecutionContext,
    ToolExecutionResult,
    ToolSpec,
)
from pickel.tools.bus import ToolBus, ToolSource
from pickel.tools.catalog import install_builtin_tools


class _EchoTool(BaseTool):
    spec = ToolSpec(
        name="echo",
        description="Echo text",
        input_schema={
            "type": "object",
            "properties": {"text": {"type": "string"}},
        },
        output_schema={"type": "string"},
    )

    async def execute(
        self,
        arguments: dict,
        context: ToolExecutionContext,
    ) -> ToolExecutionResult:
        return ToolExecutionResult(content=str(arguments.get("text", "")))


class _HiddenTool(_EchoTool):
    spec = ToolSpec(
        name="hidden",
        description="Not allowed",
        input_schema={"type": "object"},
    )


class _SafeTool(_EchoTool):
    spec = ToolSpec(
        name="safe",
        description="Safe replay tool",
        input_schema={"type": "object"},
        replay_policy="safe",
    )


def _config(tmp_path: Path) -> AppConfig:
    agent_dir = tmp_path / "agents" / "Pickle"
    skill_dir = agent_dir / "skills" / "research"
    skill_dir.mkdir(parents=True)
    (agent_dir / "AGENT.md").write_text("You are Pickle.\n", encoding="utf-8")
    (skill_dir / "SKILL.md").write_text(
        """---
name: research
description: Search carefully.
version: 2
allowed_tools: [echo]
---

# Research

Use evidence.
""",
        encoding="utf-8",
    )
    return AppConfig.model_validate(
        {
            "root": tmp_path,
            "default_agent": "Pickle",
            "default_llm": {"provider": "anthropic", "model": "claude-test"},
            "providers": {
                "anthropic": {
                    "models": {
                        "claude-test": {
                            "api_key": "secret-key",
                            "max_output_tokens": 4096,
                            "provider_options": {
                                "thinking": "high",
                                "access_token": "secret-token",
                            },
                        }
                    }
                }
            },
            "agents": {
                "Pickle": {
                    "workspace_path": ".",
                    "behavior_path": "agents/Pickle",
                    "skills_path": "agents/Pickle/skills",
                    "tools": ["echo"],
                    "extensions": ["openviking"],
                    "file_access_mode": "workspace",
                }
            },
        }
    )


def _tool_bus() -> ToolBus:
    bus = ToolBus()
    bus.register(_EchoTool(), source=ToolSource.BUILTIN, version="1")
    bus.register(_HiddenTool(), source=ToolSource.BUILTIN, version="1")
    return bus


def test_builder_preserves_tool_replay_policy(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.agents["Pickle"].tools = ["echo", "safe"]
    bus = _tool_bus()
    bus.register(_SafeTool(), source=ToolSource.BUILTIN, version="1")

    package = AgentPackageBuilder(
        app_config=config,
        tool_bus=bus,
    ).build_agent_package_version()

    assert [(tool.name, tool.replay_policy) for tool in package.tools] == [
        ("echo", "never"),
        ("safe", "safe"),
    ]


def test_pickel_package_uses_interrupt_tools_and_waits_without_legacy_cancel(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    config.agents["Pickle"].extensions = []
    config.agents["Pickle"].tools = [
        "delegate_agent",
        "send_message",
        "list_agents",
        "interrupt_agent",
        "report",
        "wait_delegation",
    ]
    bus = ToolBus()
    install_builtin_tools(bus)

    package = AgentPackageBuilder(
        app_config=config, tool_bus=bus
    ).build_agent_package_version()

    names = {tool.name for tool in package.tools}
    assert {
        "delegate_agent",
        "send_message",
        "list_agents",
        "interrupt_agent",
        "report",
        "wait_delegation",
    } <= names
    assert "cancel_delegation" not in names
    assert all(tool.output_schema is not None for tool in package.tools)


def test_builds_stable_snapshot_from_existing_pickel_settings(tmp_path: Path) -> None:
    config = _config(tmp_path)
    bus = _tool_bus()
    first = AgentPackageBuilder(
        app_config=config,
        tool_bus=bus,
        now=lambda: datetime(2026, 8, 11, tzinfo=timezone.utc),
    ).build_agent_package_version()
    second = AgentPackageBuilder(
        app_config=config,
        tool_bus=bus,
        now=lambda: datetime(2027, 1, 1, tzinfo=timezone.utc),
    ).build_agent_package_version()

    assert first.package_version_id == second.package_version_id
    assert first.behavior_instruction == "You are Pickle."
    assert [tool.name for tool in first.tools] == ["echo"]
    assert first.tools[0].input_schema["properties"]["text"]["type"] == "string"
    assert first.extensions[0].extension_id == "openviking"
    assert first.skills[0].name == "research"
    assert "Use evidence." in first.skills[0].content


def test_builder_freezes_and_loads_three_explicit_model_roles(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.agents["Pickle"].extensions = []
    catalog = config.providers["anthropic"].models
    catalog["claude-worker"] = catalog["claude-test"].model_copy(deep=True)
    catalog["claude-utility"] = catalog["claude-test"].model_copy(deep=True)
    config.agents["Pickle"].models.worker = ModelSelection(
        provider="anthropic", model="claude-worker"
    )
    config.agents["Pickle"].models.utility = ModelSelection(
        provider="anthropic", model="claude-utility"
    )
    store = InMemoryRuntimeStore()

    loaded = Boot(config, tool_bus=_tool_bus()).resolve_loaded_agent_package(
        artifact_service=ArtifactService(
            artifact_store=store,
            blob_store=InMemoryBlobStore(),
        )
    )

    assert loaded.version.model_policy.primary.model == "claude-test"
    assert loaded.version.model_policy.worker is not None
    assert loaded.version.model_policy.worker.model == "claude-worker"
    assert loaded.version.model_policy.utility is not None
    assert loaded.version.model_policy.utility.model == "claude-utility"
    assert set(loaded.model_clients) == {"primary", "worker", "utility"}


def test_rejects_missing_agent_workspace_before_runtime_starts(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.agents["Pickle"].workspace_path = tmp_path / "missing-workspace"

    with pytest.raises(ValueError, match="workspace_path 不存在"):
        AgentPackageBuilder(
            app_config=config,
            tool_bus=_tool_bus(),
        ).build_agent_package_version()


def test_snapshot_excludes_provider_secrets(tmp_path: Path) -> None:
    package = AgentPackageBuilder(
        app_config=_config(tmp_path),
        tool_bus=_tool_bus(),
    ).build_agent_package_version()

    serialized = str(package.content_dict())
    assert "secret-key" not in serialized
    assert "secret-token" not in serialized
    assert [ref.name for ref in package.model_policy.primary.required_secret_refs] == [
        "providers.anthropic.options.access_token",
        "providers.anthropic.api_key",
    ]
    assert package.model_policy.primary.provider_options == {"thinking": "high"}
    assert package.runtime_policy.max_model_steps == 8
    assert package.runtime_policy.context_turn_window == 5


def test_builder_uses_loaded_extension_version_instead_of_config_guess(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    loaded = ExtensionVersion(
        extension_id="openviking",
        implementation_ref=ImplementationRef(
            "extension", "openviking", version="9.1.0", digest="a" * 64
        ),
        version="9.1.0",
        config={"enabled": True, "endpoint": "https://loaded.example"},
    )

    package = AgentPackageBuilder(
        app_config=config,
        tool_bus=_tool_bus(),
        extension_versions={"openviking": loaded},
    ).build_agent_package_version()

    assert package.extensions == (loaded,)


def test_behavior_change_creates_new_package_version(tmp_path: Path) -> None:
    config = _config(tmp_path)
    builder = AgentPackageBuilder(app_config=config, tool_bus=_tool_bus())
    first = builder.build_agent_package_version()
    behavior_file = tmp_path / "agents" / "Pickle" / "AGENT.md"
    behavior_file.write_text("You are Pickle v2.\n", encoding="utf-8")

    second = builder.build_agent_package_version()

    assert first.package_version_id != second.package_version_id


def test_agent_package_version_round_trips_through_sqlite(tmp_path: Path) -> None:
    version = AgentPackageBuilder(
        app_config=_config(tmp_path),
        tool_bus=_tool_bus(),
    ).build_agent_package_version()
    store = SQLiteRuntimeStore(tmp_path / "packages.db")

    store.insert_agent_package_version(version)
    store.insert_agent_package_version(version)
    loaded = store.load_agent_package_version(version.package_version_id)

    assert loaded is not None
    assert loaded == version
    assert loaded.content_dict() == version.content_dict()


def test_boot_resolves_a_single_loaded_agent_package(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    config.agents["Pickle"].extensions = []
    boot = Boot(config, tool_bus=_tool_bus())
    store = InMemoryRuntimeStore()

    loaded = boot.resolve_loaded_agent_package(
        artifact_service=ArtifactService(
            artifact_store=store,
            blob_store=InMemoryBlobStore(),
        )
    )

    assert loaded.version.agent_id == "Pickle"
    assert loaded.version.behavior_instruction == "You are Pickle."
    assert loaded.tool_snapshot.names == ("echo",)
