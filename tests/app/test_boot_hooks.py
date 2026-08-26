from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from pickel.app.boot import Boot
from pickel.artifacts.artifact_service import ArtifactService
from pickel.artifacts.in_memory_blob_store import InMemoryBlobStore
from pickel.hooks.decisions import PreToolUseDecision
from pickel.hooks.events import PreToolUseEvent
from pickel.persistence.in_memory_runtime_store import InMemoryRuntimeStore
from pickel.shared.execution_identity import ExecutionIdentity
from pickel.tools.bus import ToolSnapshot
from pickel.tools.sandbox import SandboxPolicy


class _Hook:
    def __init__(self) -> None:
        self.events: list[PreToolUseEvent] = []

    async def pre_tool_use(self, event: PreToolUseEvent) -> PreToolUseDecision:
        self.events.append(event)
        return PreToolUseDecision(action="deny", reason="测试 Hook 已调用")


def _boot_for_effects() -> Boot:
    boot = object.__new__(Boot)
    boot._sandbox_policy = SandboxPolicy(enabled=False)
    boot._agent_package_builder = SimpleNamespace(
        resolve_skills_path=lambda _agent_id: None
    )
    return boot


def _loaded_package(*, hooks: tuple[object, ...]) -> SimpleNamespace:
    version = SimpleNamespace(
        agent_id="Pickle",
        workspace_policy=SimpleNamespace(file_scope="workspace"),
        model_policy=SimpleNamespace(
            primary=SimpleNamespace(provider="anthropic", model="test-model")
        ),
    )
    provider = SimpleNamespace()
    return SimpleNamespace(
        version=version,
        model_clients={"primary": provider},
        tool_snapshot=ToolSnapshot(entries=()),
        lifecycle_hooks=hooks,
        recall_sources=(),
    )


def test_build_effects_invokes_hooks_from_loaded_package(tmp_path: Path) -> None:
    handler = _Hook()
    loaded = _loaded_package(hooks=(handler,))
    store = InMemoryRuntimeStore()
    effects = _boot_for_effects()._build_effects(
        loaded_agent_package=loaded,
        artifact_service=ArtifactService(
            artifact_store=store,
            blob_store=InMemoryBlobStore(),
        ),
        session_cwd=tmp_path,
    )
    event = PreToolUseEvent(
        identity=ExecutionIdentity(
            session_id="session-1",
            operation_id="operation-1",
            step_id="step-1",
            step_sequence=1,
            tool_call_id="tool-1",
        ),
        tool_name="bash",
        arguments={"command": "pwd"},
    )

    decision = asyncio.run(effects.invoke_hook("pre_tool_use", event))

    assert decision.action == "deny"
    assert handler.events == [event]


def test_build_effects_without_hooks_is_noop_allow(tmp_path: Path) -> None:
    store = InMemoryRuntimeStore()
    effects = _boot_for_effects()._build_effects(
        loaded_agent_package=_loaded_package(hooks=()),
        artifact_service=ArtifactService(
            artifact_store=store,
            blob_store=InMemoryBlobStore(),
        ),
        session_cwd=tmp_path,
    )
    event = PreToolUseEvent(
        identity=ExecutionIdentity(
            session_id="session-1", operation_id="operation-1", tool_call_id="tool-1"
        ),
        tool_name="bash",
    )

    decision = asyncio.run(effects.invoke_hook("pre_tool_use", event))

    assert decision.action == "allow"
