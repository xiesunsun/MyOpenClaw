"""runtime 事件：tagged union + 可 JSON 序列化。"""

from __future__ import annotations

import json

from pickel.conversations.message import ToolCall
from pickel.runs.runtime_events import (
    AssistantMessageEvent,
    RuntimeEventBase,
    StepStarted,
    ToolCallCompleted,
    ToolCallStarted,
    TurnCompleted,
    TurnFailed,
    TurnStarted,
)
from pickel.runs.turn_usage import TurnUsage
from pickel.shared.event_envelope import EventEnvelope
from pickel.tools.base import ToolExecutionResult


def _envelope() -> EventEnvelope:
    return EventEnvelope(session_id="s1", turn_id="t1", step_index=1, seq=3)


def test_每个事件类型有唯一的_event_type():
    types = [
        TurnStarted, StepStarted, ToolCallStarted, ToolCallCompleted,
        AssistantMessageEvent, TurnCompleted, TurnFailed,
    ]
    values = [cls.EVENT_TYPE for cls in types]

    assert len(set(values)) == len(values)
    assert all(isinstance(v, str) and v for v in values)


def test_to_dict_含信封与_event_type():
    event = StepStarted(envelope=_envelope())
    data = event.to_dict()

    assert data["event_type"] == "step_started"
    assert data["seq"] == 3
    assert data["session_id"] == "s1"
    assert data["turn_id"] == "t1"
    assert data["step_index"] == 1
    assert "event_id" in data
    assert "occurred_at" in data


def test_occurred_at_序列化为_iso_字符串():
    data = StepStarted(envelope=_envelope()).to_dict()

    assert isinstance(data["occurred_at"], str)
    assert "T" in data["occurred_at"]


def test_所有事件都能_json_序列化():
    events: list[RuntimeEventBase] = [
        TurnStarted(envelope=_envelope(), user_text="hi"),
        StepStarted(envelope=_envelope()),
        ToolCallStarted(
            envelope=_envelope(),
            tool_call=ToolCall(id="c1", name="echo", arguments={"text": "x"}),
            batch_id="b1", call_index=0, total_calls=2,
        ),
        ToolCallCompleted(
            envelope=_envelope(),
            tool_call=ToolCall(id="c1", name="echo", arguments={"text": "x"}),
            tool_result=ToolExecutionResult(content="x"),
            batch_id="b1", call_index=0, total_calls=2,
        ),
        AssistantMessageEvent(envelope=_envelope(), text="done"),
        TurnCompleted(envelope=_envelope(), usage=TurnUsage(steps=1), elapsed_ms=120),
        TurnFailed(envelope=_envelope(), error_type="ValueError", message="boom"),
    ]

    for event in events:
        json.dumps(event.to_dict())  # 不抛异常即通过


def test_thought_signature_为_bytes_时仍可序列化():
    """gemini 的 tool_call 带 bytes 签名，直接 json.dumps 会抛 TypeError。"""
    event = ToolCallStarted(
        envelope=_envelope(),
        tool_call=ToolCall(
            id="c1", name="echo", arguments={}, thought_signature=b"\x00\x01\xff"
        ),
        batch_id="b1", call_index=0, total_calls=1,
    )

    data = event.to_dict()
    json.dumps(data)
    assert isinstance(data["tool_call"]["thought_signature"], str)


def test_tool_call_completed_携带失败信息():
    """失败不再是独立事件类型，读 is_error 即可。"""
    event = ToolCallCompleted(
        envelope=_envelope(),
        tool_call=ToolCall(id="c1", name="missing", arguments={}),
        tool_result=ToolExecutionResult(content="not found", is_error=True),
        batch_id="b1", call_index=0, total_calls=1,
    )

    assert event.to_dict()["tool_result"]["is_error"] is True


def test_turn_completed_携带_usage_合计():
    usage = TurnUsage(steps=2, input_tokens=100, cache_read_tokens=5, output_tokens=20)
    data = TurnCompleted(envelope=_envelope(), usage=usage, elapsed_ms=300).to_dict()

    assert data["usage"]["steps"] == 2
    assert data["usage"]["actual_input_tokens"] == 105


def test_turn_failed_不携带_traceback_到_dict_之外的地方():
    event = TurnFailed(
        envelope=_envelope(), error_type="ValueError",
        message="boom", traceback_text="line1\nline2",
    )
    data = event.to_dict()

    assert data["error_type"] == "ValueError"
    assert data["traceback"] == "line1\nline2"
