"""RuntimeEffects v10 副作用边界合同。"""

import asyncio
from functools import wraps

import pytest

from pickel.context.model_context import ModelContext, SystemContent
from pickel.operations.agent_run_state import (
    AgentRunState,
    ModelRequestIntent,
    ModelStepState,
)
from pickel.operations.session_operation import SessionOperation
from pickel.workspaces.workspace_binding import WorkspaceBinding
from pathlib import Path
from datetime import datetime, timezone
from pickel.providers.stream import StreamCompleted, TextDelta, ToolCallArgsDelta
from pickel.runtime.runtime_effects import ModelExecutionBoundaryError, RuntimeEffects
from pickel.tools.base import ToolExecutionResult
from pickel.conversations.agent_message import (
    AssistantMessage,
    ModelResponseMetadata,
    ModelUsage,
)


def _run_async(function):
    @wraps(function)
    def wrapper(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))

    return wrapper


class _Provider:
    async def stream(self, context):
        yield StreamCompleted(AssistantMessage())


class _StreamingProvider:
    async def stream(self, context):
        yield TextDelta("prefix")
        yield ToolCallArgsDelta("tool-1", '{"a":')
        yield ToolCallArgsDelta("tool-2", '{"b":')
        yield StreamCompleted(AssistantMessage())


class _MetadataProvider:
    async def stream(self, context):
        yield StreamCompleted(
            AssistantMessage(
                metadata=ModelResponseMetadata(
                    provider="provider",
                    model="model",
                    provider_model_version="version-1",
                    provider_response_id="response-1",
                    finish_reason="stop",
                    finish_message="done",
                    elapsed_ms=999,
                    usage=ModelUsage(input_tokens=3, output_tokens=2),
                )
            )
        )


class _ConcurrentProvider:
    def __init__(self) -> None:
        self.active = 0
        self.max_active = 0
        self.two_started = asyncio.Event()
        self.release = asyncio.Event()

    async def stream(self, context):
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        if self.active == 2:
            self.two_started.set()
        await self.release.wait()
        self.active -= 1
        yield StreamCompleted(AssistantMessage())


def _state(*, phase: str) -> AgentRunState:
    step = ModelStepState(
        step_id="step-1",
        step_sequence=1,
        phase=phase,
        request_attempt=0,
        request_intent=(
            ModelRequestIntent(
                model_context=ModelContext(system=SystemContent(), messages=()),
                context_fingerprint="test",
            )
            if phase == "request_ready"
            else None
        ),
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
async def test_stream_delta_identity_tracks_each_tool_call_without_carryover() -> None:
    effects = RuntimeEffects(provider=_StreamingProvider())
    seen = []

    async def consume(delta, identity):
        seen.append((delta, identity))

    await effects.execute_model_request(
        operation=_operation(),
        state=_state(phase="request_ready"),
        model_context=ModelContext(system=SystemContent(), messages=()),
        consume_delta=consume,
    )

    assert [item[1].session_id for item in seen] == [
        "session-1",
        "session-1",
        "session-1",
        "session-1",
    ]
    assert [item[1].operation_id for item in seen] == [
        "operation-1",
        "operation-1",
        "operation-1",
        "operation-1",
    ]
    assert [item[1].step_id for item in seen] == [
        "step-1",
        "step-1",
        "step-1",
        "step-1",
    ]
    assert [item[1].tool_call_id for item in seen] == [
        None,
        "tool-1",
        "tool-2",
        None,
    ]


@_run_async
async def test_runtime_elapsed_overrides_provider_elapsed_and_preserves_metadata(
    monkeypatch,
) -> None:
    clock = iter((100.0, 100.125, 100.25))
    monkeypatch.setattr(
        "pickel.runtime.runtime_effects.time.perf_counter", lambda: next(clock)
    )
    effects = RuntimeEffects(provider=_MetadataProvider())

    result = await effects.execute_model_request(
        operation=_operation(),
        state=_state(phase="request_ready"),
        model_context=ModelContext(system=SystemContent(), messages=()),
        context_fingerprint="fingerprint",
        hook_injected_chars=12,
    )

    metadata = result.assistant_message.metadata
    assert metadata is not None
    assert metadata.elapsed_ms == 250
    assert metadata.provider == "provider"
    assert metadata.model == "model"
    assert metadata.provider_model_version == "version-1"
    assert metadata.provider_response_id == "response-1"
    assert metadata.finish_reason == "stop"
    assert metadata.finish_message == "done"
    assert metadata.usage == ModelUsage(input_tokens=3, output_tokens=2)
    assert metadata.context_fingerprint == "fingerprint"
    assert metadata.hook_injected_chars == 12


@pytest.mark.parametrize(
    ("provider_name", "model_name", "expected_metadata"),
    [
        ("provider", "model", True),
        ("", "", False),
        ("provider", "", False),
        ("", "model", False),
    ],
)
@_run_async
async def test_runtime_creates_metadata_only_with_stable_provider_identity(
    provider_name, model_name, expected_metadata
) -> None:
    effects = RuntimeEffects(
        provider=_Provider(),
        provider_name=provider_name,
        model_name=model_name,
    )

    result = await effects.execute_model_request(
        operation=_operation(),
        state=_state(phase="request_ready"),
        model_context=ModelContext(system=SystemContent(), messages=()),
        context_fingerprint="fingerprint",
        hook_injected_chars=-1,
    )

    metadata = result.assistant_message.metadata
    assert (metadata is not None) is expected_metadata
    if metadata is not None:
        assert metadata.provider == provider_name
        assert metadata.model == model_name
        assert metadata.elapsed_ms == result.elapsed_ms
        assert metadata.context_fingerprint == "fingerprint"
        assert metadata.hook_injected_chars == 0


@_run_async
async def test_shared_package_limiter_bounds_parallel_model_requests() -> None:
    provider = _ConcurrentProvider()
    limiter = asyncio.Semaphore(2)
    effects = RuntimeEffects(
        provider=provider,
        model_request_limiter=limiter,
    )
    context = ModelContext(system=SystemContent(), messages=())
    tasks = [
        asyncio.create_task(
            effects.execute_model_request(
                operation=_operation(),
                state=_state(phase="request_ready"),
                model_context=context,
            )
        )
        for _ in range(5)
    ]

    await asyncio.wait_for(provider.two_started.wait(), timeout=1)
    assert provider.active == 2
    provider.release.set()
    await asyncio.gather(*tasks)

    assert provider.max_active == 2


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
