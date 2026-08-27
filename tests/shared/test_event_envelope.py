# tests/shared/test_event_envelope.py
"""事件信封：hook 与 runtime 共用的身份字段。"""

from __future__ import annotations

from dataclasses import replace

from pickel.hooks.events import PreToolUseEvent
from pickel.shared.event_envelope import EventEnvelope
from pickel.shared.execution_identity import ExecutionIdentity


def test_execution_identity_承载_hook_执行身份():
    identity = ExecutionIdentity(
        session_id="s1", operation_id="o1", step_id="step-1", tool_call_id="call-1"
    )

    assert identity.session_id == "s1"
    assert identity.operation_id == "o1"
    assert identity.step_sequence is None


def test_execution_identity_可关联_model_call():
    identity = ExecutionIdentity(
        session_id="s1",
        operation_id="o1",
        step_id="step-1",
        step_sequence=1,
        model_call_id="model-call-1",
    )

    assert identity.model_call_id == "model-call-1"


def test_envelope_默认_event_sequence_为未分配():
    assert EventEnvelope().event_sequence == -1


def test_envelope_组合统一执行身份():
    identity = ExecutionIdentity(
        session_id="s1",
        operation_id="o1",
        step_id="step-1",
        tool_call_id="call-1",
        message_id="message-1",
    )

    envelope = EventEnvelope(identity=identity)

    assert envelope.identity is identity
    assert not hasattr(envelope, "session_id")


def test_with_event_sequence_返回新实例且不改原件():
    original = EventEnvelope()
    assigned = original.with_event_sequence(7)

    assert assigned.event_sequence == 7
    assert original.event_sequence == -1
    assert assigned.event_id == original.event_id


def test_envelope_是_frozen():
    envelope = EventEnvelope()
    try:
        envelope.event_sequence = 3  # type: ignore[misc]
    except Exception as exc:
        assert type(exc).__name__ == "FrozenInstanceError"
    else:
        raise AssertionError("EventEnvelope 必须是 frozen")


def test_hook_事件组合_execution_identity():
    event = PreToolUseEvent(
        identity=ExecutionIdentity(session_id="s1", operation_id="t1"),
        tool_name="echo",
    )

    assert event.identity.session_id == "s1"
    assert not hasattr(event, "session_id")
    assert not hasattr(event, "event_sequence")


def test_hook_事件仍可正常构造与_replace():
    """确认改基类没破坏既有 hook 用法。"""
    event = PreToolUseEvent(
        identity=ExecutionIdentity(
            session_id="s1", operation_id="t1", step_sequence=2, tool_call_id="c1"
        ),
        tool_name="echo",
        arguments={"text": "x"},
    )
    updated = replace(
        event,
        identity=replace(event.identity, step_sequence=3),
    )

    assert updated.identity.step_sequence == 3
    assert updated.tool_name == "echo"
    assert updated.identity.tool_call_id == "c1"
