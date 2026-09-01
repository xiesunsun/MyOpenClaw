from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from pickel.app.boot import Boot
from pickel.artifacts.artifact_service import ArtifactService
from pickel.artifacts.in_memory_blob_store import InMemoryBlobStore
from pickel.conversations.content_blocks import TextBlock
from pickel.hooks.decisions import PreToolUseDecision
from pickel.hooks.events import PreToolUseEvent
from pickel.persistence.in_memory_runtime_store import InMemoryRuntimeStore
from pickel.operations.agent_run_state import (
    AgentRunState,
    ModelStepState,
    ToolCallState,
)
from pickel.operations.session_operation import SessionOperation
from pickel.shared.execution_identity import ExecutionIdentity
from pickel.tools.bus import ToolActivation, ToolBus, ToolSnapshot
from pickel.tools.catalog import install_builtin_tools
from pickel.tools.sandbox import SandboxPolicy
from pickel.workspaces.workspace_binding import WorkspaceBinding


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


def test_build_effects_thaws_nested_tool_arguments_before_execution(
    tmp_path: Path,
) -> None:
    bus = ToolBus()
    install_builtin_tools(bus)
    boot = _boot_for_effects()
    loaded = _loaded_package(hooks=())
    loaded.tool_snapshot = bus.snapshot(
        ToolActivation(allowed=frozenset({"update_plan"}))
    )
    effects = boot._build_effects(
        loaded_agent_package=loaded,
        artifact_service=ArtifactService(
            artifact_store=InMemoryRuntimeStore(),
            blob_store=InMemoryBlobStore(),
        ),
        session_cwd=tmp_path,
    )
    operation = SessionOperation(
        operation_id="operation-1",
        session_id="session-1",
        agent_package_version_id="pkg_v1",
        workspace_binding=WorkspaceBinding(
            workspace_id="workspace-1",
            working_directory=tmp_path,
            allowed_root=tmp_path,
        ),
        input_node_id="node-1",
        accepted_at=datetime.now(timezone.utc),
    )
    call = ToolCallState(
        tool_call_id="tool-1",
        tool_name="update_plan",
        arguments={
            "plan": [
                {"step": "执行嵌套参数", "status": "in_progress"},
            ]
        },
        status="intent_recorded",
        approval=None,
        replay_policy="safe",
        execution_intent=None,
        decision_reason=None,
        result_node_id=None,
        is_error=None,
    )
    state = AgentRunState(
        operation_id="operation-1",
        revision=1,
        status="running",
        waiting_reason=None,
        completed_step_count=0,
        current_step=ModelStepState(
            step_id="step-1",
            step_sequence=1,
            phase="awaiting_tools",
            request_attempt=1,
            request_intent=None,
            assistant_message_node_id="assistant-1",
            tool_calls=(call,),
        ),
        final_assistant_node_id=None,
        error=None,
        cancellation=None,
    )

    result = asyncio.run(
        effects.execute_tool_call(
            operation=operation,
            state=state,
            tool_call_id="tool-1",
        )
    )

    assert result.is_error is False
    assert result.content[0] == TextBlock(
        '{"active":true,"completed_count":0,"item_count":1,"updated":true}'
    )
