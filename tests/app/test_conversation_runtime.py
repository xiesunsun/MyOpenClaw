from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import pickel.app.conversation_runtime as conversation_runtime_module
from pickel.app.conversation_runtime import ConversationRuntime
from pickel.app.runtime_models import (
    AgentRunRequest,
    ConversationClosedError,
    OperationInProgressError,
)
from pickel.conversations.agent_message import AssistantMessage, UserMessage
from pickel.conversations.content_blocks import TextBlock
from pickel.runtime.agent_run_usage import AgentRunUsage
from pickel.runtime.event_bus import EventBus
from pickel.runtime.runtime_events import (
    AgentRunCompleted,
    AgentRunFailed,
    AssistantMessageEvent,
    ToolCallArgsDeltaEvent,
)
from pickel.providers.stream import ToolCallArgsDelta
from pickel.shared.execution_identity import ExecutionIdentity
from pickel.operations.agent_run_state import AgentRunError
from pickel.app.runtime_generation import RuntimeGeneration, RuntimeGenerationState
from pickel.runtime.agent import AgentBusyError


class _FakeInbox:
    def __init__(self) -> None:
        self.sent = []

    async def send(self, message, *, delivery):
        self.sent.append((message, delivery))
        return None


class _FakeRuntimeBus:
    def __init__(self) -> None:
        self.host_calls = SimpleNamespace(client=None)

    def close_now(self) -> None:
        return None


class _FakeAgent:
    def __init__(self, result) -> None:
        self.inbox = _FakeInbox()
        self.result = result
        self.followup_calls = []
        self.cancel_calls = []
        self.busy = False

    async def followup_and_wait(self, message, **kwargs):
        if self.busy:
            raise AgentBusyError("Agent 当前正在驱动，不能接受前台 followup")
        self.followup_calls.append((message, kwargs))
        return self.result

    def cancel(self, *, reason):
        self.cancel_calls.append(reason)


class _FakeHandle:
    def __init__(self, name: str) -> None:
        self.name = name
        self.closed = False

    def close_sync(self) -> None:
        assert not self.closed
        self.closed = True


class _FakeGeneration:
    def __init__(self) -> None:
        self.handles = []

    def acquire_loaded_package(self, package_version_id):
        handle = _FakeHandle(package_version_id)
        self.handles.append(handle)
        return handle


def _runtime(operation_result, *, generation=None, adapter_handle=None):
    runtime = object.__new__(ConversationRuntime)
    session = SimpleNamespace(session_id="session-1", active_operation_id="op-1")
    runtime._agent = _FakeAgent(
        SimpleNamespace(operation_result=operation_result, accepted=None)
    )
    runtime._session = session
    runtime._conversation_service = SimpleNamespace(
        load_conversation_session=lambda _session_id: session
    )
    runtime._events = EventBus()
    runtime._runtime_bus = _FakeRuntimeBus()
    runtime._closed = False
    runtime._runtime_generation = generation
    runtime._loaded_agent_package = SimpleNamespace(
        version=SimpleNamespace(package_version_id="package-1")
    )
    runtime._loaded_package_handle = adapter_handle
    runtime._background_tasks = set()
    runtime._event_processors = []
    runtime._unsubscribe_trace = None
    runtime._outputs = SimpleNamespace(clear=lambda: None)
    runtime._on_detach = None
    runtime._trace_sink = None
    return runtime


def _request() -> AgentRunRequest:
    return AgentRunRequest(UserMessage(content=(TextBlock("hello"),)))


def _operation_result(status: str, *, message=None, usage=None, error=None):
    return SimpleNamespace(
        operation_id="op-1",
        status=status,
        state=SimpleNamespace(error=error),
        assistant_message=message,
        usage=usage,
    )


def test_usage_is_the_same_value_on_message_completed_and_result(monkeypatch) -> None:
    # EventBus/SpanTimer 也使用同一个 perf_counter；首两个读数对应 Runtime
    # 起始与 agent_run Span，后续读数保持终点，避免观测 Span 消耗完测试时钟。
    perf_counter_values = iter((100.0, 100.0))
    monkeypatch.setattr(
        conversation_runtime_module.time,
        "perf_counter",
        lambda: next(perf_counter_values, 100.123),
    )
    usage = AgentRunUsage(steps=1, input_tokens=12, output_tokens=4, elapsed_ms=77)
    message = AssistantMessage(content=(TextBlock("done"),))
    runtime = _runtime(_operation_result("succeeded", message=message, usage=usage))
    events = []
    runtime.subscribe(events.append)

    result = asyncio.run(runtime.start_agent_run(_request()))

    assistant_event = next(
        event for event in events if isinstance(event, AssistantMessageEvent)
    )
    completed_event = next(
        event for event in events if isinstance(event, AgentRunCompleted)
    )
    assert assistant_event.usage is usage
    assert completed_event.usage is usage
    assert result.usage is usage
    assert result.elapsed_ms == 123
    assert result.elapsed_ms != usage.elapsed_ms


@pytest.mark.parametrize(
    ("operation_status", "expected_status"),
    [
        ("succeeded", "completed"),
        ("waiting", "blocked"),
        ("cancelling", "blocked"),
        ("cancelled", "cancelled"),
    ],
)
def test_operation_status_maps_to_application_status(
    operation_status: str, expected_status: str
) -> None:
    runtime = _runtime(_operation_result(operation_status))
    events = []
    runtime.subscribe(events.append)

    result = asyncio.run(runtime.start_agent_run(_request()))

    assert result.status == expected_status
    completed = next(event for event in events if isinstance(event, AgentRunCompleted))
    assert completed.outcome == expected_status


def test_missing_operation_result_is_blocked() -> None:
    runtime = _runtime(None)
    events = []
    runtime.subscribe(events.append)

    result = asyncio.run(runtime.start_agent_run(_request()))

    assert result.status == "blocked"
    completed = next(event for event in events if isinstance(event, AgentRunCompleted))
    assert completed.outcome == "blocked"


def test_tool_call_args_delta_reaches_application_event_bus() -> None:
    runtime = _runtime(_operation_result("succeeded"))
    events = []
    runtime.subscribe(events.append)

    async def followup_and_wait(message, **kwargs):
        identity = ExecutionIdentity(
            session_id="session-1",
            operation_id="op-1",
            step_id="step-1",
            tool_call_id="call-1",
        )
        await kwargs["consume_delta"](
            ToolCallArgsDelta(
                tool_call_id="call-1",
                partial_json='{"query":',
            ),
            identity,
        )
        return SimpleNamespace(
            operation_result=_operation_result("succeeded"),
            accepted=None,
        )

    runtime._agent.followup_and_wait = followup_and_wait

    asyncio.run(runtime.start_agent_run(_request()))

    delta = next(event for event in events if isinstance(event, ToolCallArgsDeltaEvent))
    assert delta.partial_json == '{"query":'
    assert delta.envelope.identity.tool_call_id == "call-1"


def test_persisted_failure_emits_failure_and_preserves_message_usage() -> None:
    usage = AgentRunUsage(steps=1, elapsed_ms=55)
    message = AssistantMessage(content=(TextBlock("partial"),))
    error = AgentRunError(
        code="ProviderError", message="provider failed", retryable=True
    )
    runtime = _runtime(
        _operation_result("failed", message=message, usage=usage, error=error)
    )
    events = []
    runtime.subscribe(events.append)

    result = asyncio.run(runtime.start_agent_run(_request()))

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.error_type == "ProviderError"
    assert result.usage is usage
    assert isinstance(events[1], AssistantMessageEvent)
    assert events[1].usage is usage
    assert isinstance(events[2], AgentRunFailed)
    assert events[2].error_type == "ProviderError"
    assert not any(isinstance(event, AgentRunCompleted) for event in events)


def test_unknown_operation_status_is_explicit_failure() -> None:
    runtime = _runtime(_operation_result("mystery"))

    result = asyncio.run(runtime.start_agent_run(_request()))

    assert result.status == "failed"
    assert result.error is not None
    assert "未知 Operation status" in result.error.message


def test_unexpected_failure_refreshes_session_before_observation() -> None:
    runtime = _runtime(_operation_result("succeeded"))
    refreshed_session = SimpleNamespace(
        session_id="session-1",
        active_operation_id="accepted-op",
        active_node_id="user-node",
    )
    runtime._conversation_service.load_conversation_session = (
        lambda _session_id: refreshed_session
    )

    async def failed_followup(message, **kwargs):
        raise NameError("missing delta type")

    runtime._agent.followup_and_wait = failed_followup

    result = asyncio.run(runtime.start_agent_run(_request()))

    assert result.status == "failed"
    assert runtime.session is refreshed_session


def test_busy_agent_maps_to_application_error_without_failed_result() -> None:
    generation = _FakeGeneration()
    runtime = _runtime(_operation_result("succeeded"), generation=generation)
    runtime._agent.busy = True
    events = []
    runtime.subscribe(events.append)

    with pytest.raises(OperationInProgressError):
        asyncio.run(runtime.start_agent_run(_request()))

    assert runtime._agent.inbox.sent == []
    assert not any(isinstance(event, AgentRunFailed) for event in events)
    assert len(generation.handles) == 1
    assert generation.handles[0].closed


@pytest.mark.parametrize("outcome", ["succeeded", "failed"])
def test_temporary_package_handle_closes_for_completed_results(outcome: str) -> None:
    generation = _FakeGeneration()
    error = AgentRunError(code="provider", message="failed", retryable=False)
    runtime = _runtime(
        _operation_result(
            outcome,
            error=error if outcome == "failed" else None,
        ),
        generation=generation,
    )

    result = asyncio.run(runtime.start_agent_run(_request()))

    assert result.status == ("failed" if outcome == "failed" else "completed")
    assert generation.handles[0].closed


def test_temporary_package_handle_closes_after_cancellation() -> None:
    generation = _FakeGeneration()
    runtime = _runtime(_operation_result("succeeded"), generation=generation)

    async def cancelled_followup(message, **kwargs):
        raise asyncio.CancelledError

    runtime._agent.followup_and_wait = cancelled_followup

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(runtime.start_agent_run(_request()))

    assert runtime._agent.cancel_calls == ["用户中断"]
    assert generation.handles[0].closed


def test_closed_conversation_does_not_acquire_temporary_package_handle() -> None:
    generation = _FakeGeneration()
    runtime = _runtime(_operation_result("succeeded"), generation=generation)
    runtime._closed = True

    with pytest.raises(ConversationClosedError):
        asyncio.run(runtime.start_agent_run(_request()))

    assert generation.handles == []


def test_detach_closes_adapter_handle_but_keeps_running_generation_alive() -> None:
    async def scenario() -> None:
        package = SimpleNamespace()
        generation = RuntimeGeneration(
            "generation-1",
            state=RuntimeGenerationState.ACTIVE,
            loaded_packages={"package-1": package},
        )
        adapter_handle = generation.acquire_loaded_package("package-1")
        runtime = _runtime(
            _operation_result("succeeded"),
            generation=generation,
            adapter_handle=adapter_handle,
        )
        started = asyncio.Event()
        release = asyncio.Event()

        async def followup_and_wait(message, **kwargs):
            started.set()
            await release.wait()
            return SimpleNamespace(
                operation_result=_operation_result("succeeded"), accepted=None
            )

        runtime._agent.followup_and_wait = followup_and_wait
        task = asyncio.create_task(runtime.start_agent_run(_request()))
        await started.wait()

        runtime.detach()
        generation.retire()
        assert adapter_handle.closed
        assert generation.operation_ref_count == 1
        assert not generation.closed

        release.set()
        await task
        await generation.wait_closed()
        assert generation.operation_ref_count == 0
        assert generation.closed

    asyncio.run(scenario())
