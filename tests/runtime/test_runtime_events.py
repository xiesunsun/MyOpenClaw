"""runtime 事件：tagged union + 可 JSON 序列化。"""

from __future__ import annotations

import json

from pickel.conversations.agent_message import ToolResultMessage
from pickel.conversations.content_blocks import TextBlock
from pickel.runtime.runtime_events import (
    AssistantMessageEvent,
    RuntimeEventBase,
    ModelStepStarted,
    ToolCallCompleted,
    ToolCallStarted,
    AgentRunCompleted,
    AgentRunFailed,
    AgentRunStarted,
    ToolCallSnapshot,
)
from pickel.runtime.agent_run_usage import AgentRunUsage
from pickel.shared.event_envelope import EventEnvelope
from pickel.shared.execution_identity import ExecutionIdentity
from pickel.tools.base import ToolExecutionResult


def _envelope() -> EventEnvelope:
    return EventEnvelope(
        identity=ExecutionIdentity(
            session_id="s1",
            operation_id="t1",
            step_sequence=1,
        ),
        event_sequence=3,
    )


def test_每个事件类型有唯一的_event_type():
    types = [
        AgentRunStarted,
        ModelStepStarted,
        ToolCallStarted,
        ToolCallCompleted,
        AssistantMessageEvent,
        AgentRunCompleted,
        AgentRunFailed,
    ]
    values = [cls.EVENT_TYPE for cls in types]

    assert len(set(values)) == len(values)
    assert all(isinstance(v, str) and v for v in values)


def test_to_dict_含信封与_event_type():
    event = ModelStepStarted(envelope=_envelope())
    data = event.to_dict()

    assert data["event_type"] == "model_step_started"
    assert data["event_sequence"] == 3
    assert data["session_id"] == "s1"
    assert data["operation_id"] == "t1"
    assert data["step_sequence"] == 1
    assert "event_id" in data
    assert "occurred_at" in data


def test_occurred_at_序列化为_iso_字符串():
    data = ModelStepStarted(envelope=_envelope()).to_dict()

    assert isinstance(data["occurred_at"], str)
    assert "T" in data["occurred_at"]


def test_所有事件都能_json_序列化():
    events: list[RuntimeEventBase] = [
        AgentRunStarted(envelope=_envelope(), user_text="hi"),
        ModelStepStarted(envelope=_envelope()),
        ToolCallStarted(
            envelope=_envelope(),
            tool_call=ToolCallSnapshot(
                tool_call_id="c1", tool_name="echo", arguments={"text": "x"}
            ),
            batch_id="b1",
            call_index=0,
            total_calls=2,
        ),
        ToolCallCompleted(
            envelope=_envelope(),
            tool_call=ToolCallSnapshot(
                tool_call_id="c1", tool_name="echo", arguments={"text": "x"}
            ),
            tool_result=ToolExecutionResult(content="x"),
            tool_result_message=ToolResultMessage(
                tool_call_id="c1",
                tool_name="echo",
                content=[TextBlock(text="x")],
                structured_content={"value": "x"},
            ),
            batch_id="b1",
            call_index=0,
            total_calls=2,
        ),
        AssistantMessageEvent(envelope=_envelope(), text="done"),
        AgentRunCompleted(
            envelope=_envelope(), usage=AgentRunUsage(steps=1), elapsed_ms=120
        ),
        AgentRunFailed(envelope=_envelope(), error_type="ValueError", message="boom"),
    ]

    for event in events:
        json.dumps(event.to_dict())  # 不抛异常即通过


def test_tool_call_completed_携带失败信息():
    """失败不再是独立事件类型，读 is_error 即可。"""
    event = ToolCallCompleted(
        envelope=_envelope(),
        tool_call=ToolCallSnapshot(
            tool_call_id="c1", tool_name="missing", arguments={}
        ),
        tool_result=ToolExecutionResult(content="not found", is_error=True),
        batch_id="b1",
        call_index=0,
        total_calls=1,
    )

    assert event.to_dict()["tool_result"]["is_error"] is True


def test_tool_call_completed_携带模型实际看到的结果消息():
    event = ToolCallCompleted(
        envelope=_envelope(),
        tool_result=ToolExecutionResult(content="ok", metadata={"runtime": True}),
        tool_result_message=ToolResultMessage(
            tool_call_id="c1",
            tool_name="lookup",
            content=[TextBlock(text="ok")],
            structured_content={"id": 7},
        ),
    )

    payload = event.to_dict()
    assert payload["tool_result"]["metadata"] == {"runtime": True}
    assert payload["tool_result_message"]["structured_content"] == {"id": 7}


def test_agent_run_completed_携带_usage_合计():
    usage = AgentRunUsage(
        steps=2, input_tokens=100, cache_read_tokens=5, output_tokens=20
    )
    data = AgentRunCompleted(
        envelope=_envelope(), usage=usage, elapsed_ms=300
    ).to_dict()

    assert data["usage"]["steps"] == 2
    assert data["usage"]["actual_input_tokens"] == 105


def test_agent_run_failed_不携带_traceback_到_dict_之外的地方():
    event = AgentRunFailed(
        envelope=_envelope(),
        error_type="ValueError",
        message="boom",
        traceback_text="line1\nline2",
    )
    data = event.to_dict()

    assert data["error_type"] == "ValueError"
    assert data["traceback"] == "line1\nline2"


def test_delta_事件的_event_type_唯一且不与既有冲突():
    from pickel.runtime.runtime_events import (
        TextDeltaEvent,
        ThinkingDeltaEvent,
        ToolCallArgsDeltaEvent,
        AgentRunInterrupted,
    )

    new_types = [
        ThinkingDeltaEvent,
        TextDeltaEvent,
        ToolCallArgsDeltaEvent,
        AgentRunInterrupted,
    ]
    old_types = [
        AgentRunStarted,
        ModelStepStarted,
        ToolCallStarted,
        ToolCallCompleted,
        AssistantMessageEvent,
        AgentRunCompleted,
        AgentRunFailed,
    ]
    values = [cls.EVENT_TYPE for cls in new_types + old_types]

    assert len(set(values)) == len(values)


def test_delta_事件可_json_序列化():
    from pickel.runtime.runtime_events import (
        TextDeltaEvent,
        ThinkingDeltaEvent,
        ToolCallArgsDeltaEvent,
        AgentRunInterrupted,
    )

    events = [
        ThinkingDeltaEvent(envelope=_envelope(), text="想"),
        TextDeltaEvent(envelope=_envelope(), text="你好"),
        ToolCallArgsDeltaEvent(
            envelope=_envelope(), tool_call_id="c1", partial_json='{"a"'
        ),
        AgentRunInterrupted(envelope=_envelope(), at_step=2, partial_text="写到一半"),
    ]

    for event in events:
        data = event.to_dict()
        json.dumps(data)
        assert data["event_sequence"] == 3


def test_text_delta_事件载荷():
    from pickel.runtime.runtime_events import TextDeltaEvent

    data = TextDeltaEvent(envelope=_envelope(), text="你好").to_dict()

    assert data["event_type"] == "text_delta"
    assert data["text"] == "你好"


def test_tool_call_args_delta_事件载荷():
    from pickel.runtime.runtime_events import ToolCallArgsDeltaEvent

    data = ToolCallArgsDeltaEvent(
        envelope=_envelope(), tool_call_id="c1", partial_json='{"a": 1}'
    ).to_dict()

    assert data["event_type"] == "tool_call_args_delta"
    assert data["tool_call_id"] == "c1"
    assert data["partial_json"] == '{"a": 1}'


def test_agent_run_interrupted_载荷():
    from pickel.runtime.runtime_events import AgentRunInterrupted

    data = AgentRunInterrupted(
        envelope=_envelope(), at_step=2, partial_text="写到一半"
    ).to_dict()

    assert data["event_type"] == "agent_run_interrupted"
    assert data["at_step"] == 2
    assert data["partial_text"] == "写到一半"
