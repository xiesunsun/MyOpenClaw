# tests/shared/test_event_envelope.py
"""事件信封：hook 与 runtime 共用的身份字段。"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

from pickel.hooks.events import PreToolUseEvent
from pickel.shared.event_envelope import EventEnvelope, EventIdentity


def test_identity_字段齐全且有默认值():
    identity = EventIdentity()

    assert identity.event_id
    assert identity.session_id == ""
    assert identity.operation_id == ""
    assert identity.step_sequence is None
    assert identity.occurred_at.tzinfo is timezone.utc


def test_每个_identity_的_event_id_唯一():
    assert EventIdentity().event_id != EventIdentity().event_id


def test_envelope_默认_event_sequence_为未分配():
    assert EventEnvelope().event_sequence == -1


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


def test_hook_事件继承_identity():
    """hook 事件复用同一组身份字段，但不带 event_sequence。"""
    event = PreToolUseEvent(session_id="s1", operation_id="t1", tool_name="echo")

    assert isinstance(event, EventIdentity)
    assert event.session_id == "s1"
    assert not hasattr(event, "event_sequence")


def test_hook_事件仍可正常构造与_replace():
    """确认改基类没破坏既有 hook 用法。"""
    event = PreToolUseEvent(
        session_id="s1",
        operation_id="t1",
        step_sequence=2,
        tool_name="echo",
        tool_call_id="c1",
        arguments={"text": "x"},
    )
    updated = replace(event, step_sequence=3)

    assert updated.step_sequence == 3
    assert updated.tool_name == "echo"
    assert isinstance(updated.occurred_at, datetime)
