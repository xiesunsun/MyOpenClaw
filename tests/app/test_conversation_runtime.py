from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import pickel.app.conversation_runtime as conversation_runtime_module
from pickel.app.conversation_runtime import ConversationRuntime
from pickel.app.runtime_models import AgentRunRequest
from pickel.conversations.agent_message import AssistantMessage, UserMessage
from pickel.conversations.content_blocks import TextBlock
from pickel.runtime.agent_run_usage import AgentRunUsage
from pickel.runtime.event_bus import EventBus
from pickel.runtime.runtime_events import (
    AgentRunCompleted,
    AgentRunFailed,
    AssistantMessageEvent,
)
from pickel.operations.agent_run_state import AgentRunError


class _FakeInbox:
    async def send(self, message, *, delivery):
        return None


class _FakeAgent:
    def __init__(self, result) -> None:
        self.inbox = _FakeInbox()
        self.result = result

    async def when_idle(self, **kwargs):
        return self.result

    def cancel(self, *, reason):
        raise AssertionError(f"不应取消正常测试运行: {reason}")


def _runtime(operation_result):
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
    runtime._runtime_bus = SimpleNamespace(host_calls=SimpleNamespace(client=None))
    runtime._closed = False
    runtime._active_task = None
    runtime._active_operation_id = None
    runtime._control_lock = asyncio.Lock()
    runtime._release_package_after_task = False
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
    perf_counter_values = iter((100.0, 100.0, 100.123, 100.123))
    monkeypatch.setattr(
        conversation_runtime_module.time,
        "perf_counter",
        lambda: next(perf_counter_values),
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
