"""RuntimeEffects 在 Stage 10 只发送已冻结 PreparedModelCall。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from pickel.conversations.agent_message import (
    AssistantMessage,
    ModelResponseMetadata,
    ModelUsage,
    ToolResultMessage,
)
from pickel.model_calls.prepared import PreparedModelCall
from pickel.operations.agent_run_state import AgentRunState, ModelStepState
from pickel.operations.session_operation import SessionOperation
from pickel.providers.stream import StreamCompleted, TextDelta, ToolCallArgsDelta
from pickel.runtime.runtime_effects import RuntimeEffects
from pickel.shared.execution_identity import ExecutionIdentity
from pickel.workspaces.workspace_binding import WorkspaceBinding


class _Provider:
    async def stream_prepared(self, prepared):
        yield StreamCompleted(
            AssistantMessage(),
            provider_response={"request": prepared.requested_model},
            http_status=200,
        )


class _StreamingProvider:
    async def stream_prepared(self, prepared):
        del prepared
        yield TextDelta("prefix")
        yield ToolCallArgsDelta("tool-1", '{"a":')
        yield ToolCallArgsDelta("tool-2", '{"b":')
        yield StreamCompleted(AssistantMessage(), provider_response={"id": "r1"})


class _MetadataProvider:
    async def stream_prepared(self, prepared):
        del prepared
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
            ),
            provider_response={"id": "response-1"},
            http_status=200,
        )


class _ConcurrentProvider:
    def __init__(self) -> None:
        self.active = 0
        self.max_active = 0
        self.two_started = asyncio.Event()
        self.release = asyncio.Event()

    async def stream_prepared(self, prepared):
        del prepared
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        if self.active == 2:
            self.two_started.set()
        await self.release.wait()
        self.active -= 1
        yield StreamCompleted(AssistantMessage(), provider_response={})


def _prepared() -> PreparedModelCall:
    return PreparedModelCall(
        provider="provider",
        api_kind="test-wire",
        endpoint="responses",
        requested_model="model",
        body={"model": "model", "stream": True},
    )


def _identity() -> ExecutionIdentity:
    return ExecutionIdentity(
        session_id="session-1",
        operation_id="operation-1",
        step_id="step-1",
        step_sequence=1,
    )


def test_stream_delta_identity_tracks_each_tool_call_without_carryover() -> None:
    effects = RuntimeEffects(provider=_StreamingProvider())
    seen = []

    async def consume(delta, identity):
        seen.append((delta, identity))

    asyncio.run(
        effects.execute_prepared_model_call(
            prepared=_prepared(),
            identity=_identity(),
            consume_delta=consume,
        )
    )

    assert [item[1].tool_call_id for item in seen] == [
        None,
        "tool-1",
        "tool-2",
        None,
    ]


def test_runtime_elapsed_overrides_provider_elapsed_and_preserves_metadata(
    monkeypatch,
) -> None:
    clock = iter((100.0, 100.125, 100.25))
    monkeypatch.setattr(
        "pickel.runtime.runtime_effects.time.perf_counter", lambda: next(clock)
    )
    effects = RuntimeEffects(provider=_MetadataProvider())

    result = asyncio.run(
        effects.execute_prepared_model_call(
            prepared=_prepared(),
            identity=_identity(),
            context_fingerprint="fingerprint",
            hook_injected_chars=12,
        )
    )

    metadata = result.assistant_message.metadata
    assert metadata is not None
    assert metadata.elapsed_ms == 125
    assert metadata.provider == "provider"
    assert metadata.model == "model"
    assert metadata.provider_model_version == "version-1"
    assert metadata.provider_response_id == "response-1"
    assert metadata.finish_reason == "stop"
    assert metadata.finish_message == "done"
    assert metadata.usage == ModelUsage(input_tokens=3, output_tokens=2)
    assert metadata.context_fingerprint == "fingerprint"
    assert metadata.hook_injected_chars == 12
    assert result.provider_response == {"id": "response-1"}
    assert result.http_status == 200


def test_shared_package_limiter_bounds_parallel_model_requests() -> None:
    provider = _ConcurrentProvider()
    limiter = asyncio.Semaphore(2)
    effects = RuntimeEffects(provider=provider, model_request_limiter=limiter)

    async def run() -> None:
        tasks = [
            asyncio.create_task(
                effects.execute_prepared_model_call(
                    prepared=_prepared(),
                    identity=_identity(),
                )
            )
            for _ in range(5)
        ]
        await asyncio.wait_for(provider.two_started.wait(), timeout=1)
        assert provider.active == 2
        provider.release.set()
        await asyncio.gather(*tasks)

    asyncio.run(run())
    assert provider.max_active == 2


def test_tool_effect_requires_intent_recorded_state(tmp_path) -> None:
    called = False

    async def execute_tool(**kwargs):
        nonlocal called
        called = True
        return ToolResultMessage(tool_call_id="tool-1", tool_name="echo")

    effects = RuntimeEffects(provider=_Provider(), execute_tool=execute_tool)
    step = ModelStepState("step-1", 1, "preparing_request", 0, None, None, ())
    state = AgentRunState(
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
    operation = SessionOperation(
        operation_id="operation-1",
        session_id="session-1",
        agent_package_version_id="agentpkg_" + "a" * 64,
        workspace_binding=WorkspaceBinding(
            workspace_id="workspace-1",
            working_directory=tmp_path,
            allowed_root=tmp_path,
        ),
        input_node_id="node-1",
        accepted_at=datetime.now(timezone.utc),
    )

    with pytest.raises(RuntimeError, match="intent_recorded"):
        asyncio.run(
            effects.execute_tool_call(
                operation=operation,
                state=state,
                tool_call_id="tool-1",
            )
        )
    assert called is False


def test_runtime_effects_has_no_model_context_send_or_request_snapshot_path() -> None:
    assert not hasattr(RuntimeEffects, "execute_model_request")
    assert not hasattr(RuntimeEffects, "_record_request_snapshot")
