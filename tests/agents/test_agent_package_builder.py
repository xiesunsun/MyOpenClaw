from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from pickel.agents.agent_package_builder import AgentPackageBuilder
from pickel.app.boot import Boot
from pickel.config.app_config import AppConfig
from pickel.persistence.sqlite_runtime_store import SQLiteRuntimeStore
from pickel.tools.base import (
    BaseTool,
    ToolExecutionContext,
    ToolExecutionResult,
    ToolSpec,
)
from pickel.tools.bus import ToolBus, ToolSource


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


def test_builds_stable_snapshot_from_existing_pickel_settings(tmp_path: Path) -> None:
    config = _config(tmp_path)
    bus = _tool_bus()
    first = AgentPackageBuilder(
        app_config=config,
        tool_bus=bus,
        now=lambda: datetime(2026, 8, 11, tzinfo=timezone.utc),
    ).build_loaded_agent_package()
    second = AgentPackageBuilder(
        app_config=config,
        tool_bus=bus,
        now=lambda: datetime(2027, 1, 1, tzinfo=timezone.utc),
    ).build_loaded_agent_package()

    assert first.version.package_version_id == second.version.package_version_id
    assert first.version.digest == second.version.digest
    assert first.version.behavior_instruction == "You are Pickle."
    assert first.definition.tool_ids == ("echo",)
    assert first.definition.extension_ids == ("openviking",)
    assert [tool.name for tool in first.version.tools] == ["echo"]
    assert first.version.tools[0].input_schema["properties"]["text"]["type"] == "string"
    assert first.version.skills[0].name == "research"
    assert "Use evidence." in first.version.skills[0].content
    assert first.tool_snapshot.names == ("echo",)


def test_snapshot_excludes_provider_secrets(tmp_path: Path) -> None:
    package = AgentPackageBuilder(
        app_config=_config(tmp_path),
        tool_bus=_tool_bus(),
    ).build_loaded_agent_package()

    serialized = str(package.version.content_dict())
    assert "secret-key" not in serialized
    assert "secret-token" not in serialized
    assert "access_token" not in serialized
    assert package.version.model.required_secrets == ("api_key",)
    assert package.version.model.provider_options == {"thinking": "high"}
    assert package.version.runtime.max_model_steps == 8
    assert package.version.runtime.context_unit_window == 5


def test_behavior_change_creates_new_package_version(tmp_path: Path) -> None:
    config = _config(tmp_path)
    builder = AgentPackageBuilder(app_config=config, tool_bus=_tool_bus())
    first = builder.build_loaded_agent_package()
    behavior_file = tmp_path / "agents" / "Pickle" / "AGENT.md"
    behavior_file.write_text("You are Pickle v2.\n", encoding="utf-8")

    second = builder.build_loaded_agent_package()

    assert first.version.package_version_id != second.version.package_version_id
    assert first.version.digest != second.version.digest


def test_agent_package_version_round_trips_through_sqlite(tmp_path: Path) -> None:
    version = (
        AgentPackageBuilder(
            app_config=_config(tmp_path),
            tool_bus=_tool_bus(),
        )
        .build_loaded_agent_package()
        .version
    )
    store = SQLiteRuntimeStore(tmp_path / "packages.db")

    store.insert_agent_package_version(version)
    store.insert_agent_package_version(version)
    loaded = store.load_agent_package_version(version.package_version_id)

    assert loaded is not None
    assert loaded == version
    assert loaded.content_dict() == version.content_dict()


def test_boot_reuses_agent_package_builder_for_existing_resolve_agent(
    tmp_path: Path,
) -> None:
    boot = Boot(_config(tmp_path), tool_bus=_tool_bus())

    loaded = boot.resolve_loaded_agent_package()
    agent = boot.resolve_agent()

    assert agent.agent_id == loaded.definition.agent_id
    assert agent.behavior_instruction == loaded.version.behavior_instruction
    assert agent.tool_ids == list(loaded.definition.tool_ids)
