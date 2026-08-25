"""RuntimeEffects v10 副作用边界合同。"""

import asyncio
from functools import wraps

import pytest

from pickel.context.model_context import ModelContext, SystemContent
from pickel.operations.agent_run_state import AgentRunState, ModelStepState
from pickel.operations.session_operation import SessionOperation
from pickel.workspaces.workspace_binding import WorkspaceBinding
from pathlib import Path
from datetime import datetime, timezone
from pickel.providers.stream import StreamCompleted
from pickel.runtime.runtime_effects import ModelExecutionBoundaryError, RuntimeEffects
from pickel.tools.base import ToolExecutionResult
from pickel.conversations.agent_message import AssistantMessage


def _run_async(function):
    @wraps(function)
    def wrapper(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))

    return wrapper


class _Provider:
    async def stream(self, context):
        yield StreamCompleted(AssistantMessage())


def _state(*, phase: str) -> AgentRunState:
    step = ModelStepState(
        step_id="step-1",
        step_sequence=1,
        phase=phase,
        request_attempt=0,
        request_intent=None,
        assistant_message_node_id=None,
        tool_calls=(),
    )
    return AgentRunState(
        operation_id="operation-1",
        revision=1,
        status="running",
        waiting_reason=None,
        completed_step_count=0,
        current_step=step,
        final_assistant_node_id=None,
        error=None,
        cancellation=None,
    )


@_run_async
async def test_provider_request_requires_persisted_request_intent() -> None:
    effects = RuntimeEffects(provider=_Provider())
    context = ModelContext(system=SystemContent(), messages=())

    with pytest.raises(ModelExecutionBoundaryError):
        await effects.execute_model_request(
            operation=_operation(),
            state=_state(phase="preparing_request"),
            model_context=context,
        )


@_run_async
async def test_tool_effect_requires_intent_recorded_state() -> None:
    called = False

    async def execute_tool(**kwargs):
        nonlocal called
        called = True
        return ToolExecutionResult(content="ok")

    effects = RuntimeEffects(provider=_Provider(), execute_tool=execute_tool)
    with pytest.raises(RuntimeError, match="intent_recorded"):
        await effects.execute_tool_call(
            operation=_operation(),
            state=_state(phase="preparing_request"),
            tool_call_id="tool-1",
        )
    assert called is False


def _operation() -> SessionOperation:
    return SessionOperation(
        operation_id="operation-1",
        session_id="session-1",
        agent_package_version_id="agentpkg_" + "a" * 64,
        workspace_binding=WorkspaceBinding(
            workspace_id="workspace-1",
            working_directory=Path.cwd(),
            allowed_root=Path.cwd(),
        ),
        input_node_id="node-1",
        accepted_at=datetime.now(timezone.utc),
    )
