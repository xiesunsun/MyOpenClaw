"""WorkerCallSender 的窄合同：逐次落库、失败记账与有界退避重试。"""

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from pickel.agents.agent_package import AgentRuntimePolicy
from pickel.conversations.agent_message import AssistantMessage
from pickel.conversations.content_blocks import TextBlock
from pickel.context.model_context import ModelContext, SystemContent
from pickel.model_calls.model_call import ModelCall
from pickel.shared.execution_identity import ExecutionIdentity
from pickel.providers.errors import ProviderStreamIncompleteError
from pickel.model_calls.service import ModelCallResponse
from pickel.runtime.model_call_send_gate import ModelCallSendFailure
from pickel.runtime.worker_call_sender import WorkerCallSendError, WorkerCallSender

NOW = datetime(2026, 8, 29, tzinfo=timezone.utc)


def _policy() -> AgentRuntimePolicy:
    return AgentRuntimePolicy(max_model_steps=8)


def _context() -> ModelContext:
    return ModelContext(system=SystemContent(), messages=())


class _FakeModelCalls:
    """只实现 WorkerCallSender 所需窄面的 ModelCallService fake。"""

    def __init__(self) -> None:
        self.prepared: list[ModelCall] = []
        self.failures: list[tuple[ModelCall, Exception]] = []
        self.completed: list[ModelCall] = []
        self._sequence = 0

    def prepare_session_call(
        self, *, session_id, context, mapper, request_attempt, model_role, purpose
    ):
        del context, mapper
        self._sequence += 1
        call = ModelCall(
            model_call_id=f"worker-{self._sequence}",
            identity=ExecutionIdentity(session_id=session_id),
            request_attempt=request_attempt,
            model_role=model_role,
            purpose=purpose,
            provider="worker",
            api_kind="test",
            endpoint="test",
            requested_model="worker-model",
            returned_model=None,
            status="in_flight",
            request_content_ref="request",
            response_content_ref=None,
            context_fingerprint=None,
            provider_request_id=None,
            http_status=None,
            error=None,
            created_at=NOW,
            started_at=NOW,
            first_chunk_at=None,
            finished_at=None,
        )
        self.prepared.append(call)
        prepared = SimpleNamespace(provider="worker", requested_model="worker-model")
        return SimpleNamespace(model_call=call, prepared=prepared)

    def record_send_failure(self, call, cause, *, first_chunk_at=None):
        del first_chunk_at
        self.failures.append((call, cause))
        failed = SimpleNamespace(
            model_call_id=call.model_call_id,
            request_attempt=call.request_attempt,
            status=(
                "incomplete"
                if isinstance(cause, ProviderStreamIncompleteError)
                else "failed"
            ),
        )
        return failed

    def complete_session_response(self, *, call, response):
        del response
        self.completed.append(call)


class _FakeGate:
    def __init__(self, outcomes) -> None:
        self._outcomes = list(outcomes)
        self.sent: list[ModelCall] = []

    async def send(self, *, call, prepared, effects, consume_delta=None):
        del prepared, effects, consume_delta
        self.sent.append(call)
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            if isinstance(outcome, ModelCallSendFailure):
                raise outcome
            raise ModelCallSendFailure(call=call, cause=outcome, first_chunk_at=None)
        return outcome


def _completed() -> ModelCallResponse:
    return ModelCallResponse(
        assistant_message=AssistantMessage((TextBlock("worker 摘要"),)),
        provider_response={"ok": True},
        started_at=NOW,
        first_chunk_at=None,
        finished_at=NOW,
        http_status=200,
    )


async def _record_sleep(sleeps: list[float], seconds: float) -> None:
    sleeps.append(seconds)


def test_worker_call_sender_sends_once_and_completes_on_success():
    model_calls = _FakeModelCalls()
    gate = _FakeGate([_completed()])
    sender = WorkerCallSender(model_calls=model_calls, send_gate=gate)

    message = asyncio.run(
        sender(
            session_id="session-1",
            context=_context(),
            purpose="history_compaction",
            worker_provider=object(),
            runtime_policy=_policy(),
            provider_timeout_seconds=30.0,
        )
    )

    assert message.content[0].text == "worker 摘要"
    assert len(model_calls.prepared) == 1
    assert model_calls.prepared[0].model_role == "worker"
    assert model_calls.prepared[0].purpose == "history_compaction"
    assert model_calls.completed == [model_calls.prepared[0]]
    assert model_calls.failures == []


def test_worker_call_sender_retries_with_policy_backoff_then_succeeds():
    model_calls = _FakeModelCalls()
    gate = _FakeGate([TimeoutError("transient"), _completed()])
    sleeps: list[float] = []
    sender = WorkerCallSender(
        model_calls=model_calls,
        send_gate=gate,
        sleep=lambda seconds: _record_sleep(sleeps, seconds),
    )

    message = asyncio.run(
        sender(
            session_id="session-1",
            context=_context(),
            purpose="goal_verification",
            worker_provider=object(),
            runtime_policy=_policy(),
            provider_timeout_seconds=30.0,
        )
    )

    assert message is not None
    assert [call.request_attempt for call in model_calls.prepared] == [1, 2]
    # 第一次失败后按 completed_attempts=1 取退避表：15000ms。
    assert sleeps == [15.0]
    assert len(model_calls.failures) == 1
    assert len(model_calls.completed) == 1


def test_worker_call_sender_raises_after_retry_exhaustion():
    model_calls = _FakeModelCalls()
    gate = _FakeGate([TimeoutError("a"), TimeoutError("b")])
    sleeps: list[float] = []
    sender = WorkerCallSender(
        model_calls=model_calls,
        send_gate=gate,
        sleep=lambda seconds: _record_sleep(sleeps, seconds),
    )

    with pytest.raises(WorkerCallSendError):
        asyncio.run(
            sender(
                session_id="session-1",
                context=_context(),
                purpose="history_compaction",
                worker_provider=object(),
                runtime_policy=_policy(),
                provider_timeout_seconds=30.0,
            )
        )

    # 默认 worker 额度 2 次；每次失败都记账，第二次失败后不再退避。
    assert [call.request_attempt for call in model_calls.prepared] == [1, 2]
    assert len(model_calls.failures) == 2
    assert sleeps == [15.0]


def test_worker_call_sender_does_not_retry_after_first_output():
    """已收到输出的不完整流不可重试：直接抛出，记账为 incomplete。"""
    model_calls = _FakeModelCalls()
    cause = ProviderStreamIncompleteError(
        message="不完整流",
        assistant_message=AssistantMessage(()),
        provider_response={},
        http_status=200,
    )
    call = ModelCall(
        model_call_id="worker-partial",
        identity=ExecutionIdentity(session_id="session-1"),
        request_attempt=1,
        model_role="worker",
        purpose="history_compaction",
        provider="worker",
        api_kind="test",
        endpoint="test",
        requested_model="worker-model",
        returned_model=None,
        status="in_flight",
        request_content_ref="request",
        response_content_ref=None,
        context_fingerprint=None,
        provider_request_id=None,
        http_status=None,
        error=None,
        created_at=NOW,
        started_at=NOW,
        first_chunk_at=NOW,
        finished_at=None,
    )
    gate = _FakeGate([ModelCallSendFailure(call=call, cause=cause, first_chunk_at=NOW)])
    sender = WorkerCallSender(
        model_calls=model_calls,
        send_gate=gate,
        sleep=lambda seconds: (_ for _ in ()).throw(AssertionError("不应退避")),
    )

    with pytest.raises(WorkerCallSendError):
        asyncio.run(
            sender(
                session_id="session-1",
                context=_context(),
                purpose="history_compaction",
                worker_provider=object(),
                runtime_policy=_policy(),
                provider_timeout_seconds=30.0,
            )
        )

    assert len(model_calls.prepared) == 1
    assert model_calls.failures[0][1] is cause
