from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from pickel.conversations.agent_message import AssistantMessage
from pickel.conversations.content_blocks import TextBlock
from pickel.model_calls.model_call import ModelCall
from pickel.model_calls.prepared import PreparedModelCall
from pickel.providers.stream import StreamCompleted
from pickel.runtime.model_call_send_gate import (
    ModelCallSendConflict,
    ModelCallSendGate,
)
from pickel.runtime.runtime_effects import RuntimeEffects
from pickel.shared.execution_identity import ExecutionIdentity

NOW = datetime(2026, 8, 27, tzinfo=timezone.utc)


class _Store:
    def __init__(self, *, cas: bool = True) -> None:
        self.cas = cas
        self.transitions: list[ModelCall] = []

    def transition_model_call(self, *, model_call, expected_status):
        assert expected_status == "prepared"
        if not self.cas:
            return False
        self.transitions.append(model_call)
        return True


class _Provider:
    def __init__(self) -> None:
        self.calls = 0
        self.bodies = []

    async def stream_prepared(self, prepared):
        self.calls += 1
        self.bodies.append(prepared.body)
        yield StreamCompleted(
            AssistantMessage((TextBlock("ok"),)),
            provider_response={"id": "response-1"},
            http_status=200,
        )


def _call() -> ModelCall:
    return ModelCall(
        model_call_id="call-1",
        identity=ExecutionIdentity(
            session_id="session-1",
            operation_id="operation-1",
            step_id="step-1",
            step_sequence=1,
        ),
        request_attempt=1,
        model_role="primary",
        purpose="agent_step",
        provider="test",
        api_kind="test-wire",
        endpoint="responses",
        requested_model="model",
        returned_model=None,
        status="prepared",
        request_content_ref="content-ref",
        response_content_ref=None,
        context_fingerprint="fingerprint",
        provider_request_id=None,
        http_status=None,
        error=None,
        created_at=NOW,
        started_at=None,
        first_chunk_at=None,
        finished_at=None,
    )


def _prepared() -> PreparedModelCall:
    return PreparedModelCall(
        provider="test",
        api_kind="test-wire",
        endpoint="responses",
        requested_model="model",
        body={"model": "model", "stream": True, "input": ["same-body"]},
    )


def test_cas_failure_never_calls_provider() -> None:
    store = _Store(cas=False)
    provider = _Provider()
    gate = ModelCallSendGate(store, now=lambda: NOW)

    with pytest.raises(ModelCallSendConflict):
        asyncio.run(
            gate.send(
                call=_call(),
                prepared=_prepared(),
                effects=RuntimeEffects(provider=provider),
            )
        )

    assert provider.calls == 0


def test_gate_sends_exact_prepared_body_after_in_flight_cas() -> None:
    store = _Store()
    provider = _Provider()
    prepared = _prepared()
    gate = ModelCallSendGate(store, now=lambda: NOW)

    response = asyncio.run(
        gate.send(
            call=_call(),
            prepared=prepared,
            effects=RuntimeEffects(provider=provider),
        )
    )

    assert provider.calls == 1
    assert provider.bodies == [prepared.body]
    assert store.transitions == [replace(_call(), status="in_flight", started_at=NOW)]
    assert response.provider_response == {"id": "response-1"}
