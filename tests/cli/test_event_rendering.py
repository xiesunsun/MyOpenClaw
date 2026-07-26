"""事件渲染：新事件类型下终端输出不变。"""

from __future__ import annotations

import asyncio

from rich.console import Console

from pickel.cli.event_renderer import ChatEventRenderer
from pickel.conversations.message import ToolCall
from pickel.runs.runtime_events import (
    AssistantMessageEvent,
    StepStarted,
    ToolCallCompleted,
    ToolCallStarted,
    TurnCompleted,
    TurnStarted,
)
from pickel.runs.turn_usage import TurnUsage
from pickel.shared.event_envelope import EventEnvelope
from pickel.tools.base import ToolExecutionResult


def _render(event) -> str:
    console = Console(width=100, record=True, force_terminal=False)
    asyncio.run(ChatEventRenderer(console).handle_event(event))
    return console.export_text()


def test_step_started_显示步数():
    text = _render(StepStarted(envelope=EventEnvelope(step_index=2)))

    assert "Step 2" in text


def test_tool_call_started_显示名称与参数():
    text = _render(
        ToolCallStarted(
            tool_call=ToolCall(id="c1", name="echo", arguments={"text": "hi"}),
            batch_id="b1", call_index=0, total_calls=1,
        )
    )

    assert "echo" in text
    assert "running" in text


def test_tool_call_completed_成功显示_ok():
    text = _render(
        ToolCallCompleted(
            tool_call=ToolCall(id="c1", name="echo", arguments={}),
            tool_result=ToolExecutionResult(content="done"),
        )
    )

    assert "ok" in text
    assert "failed" not in text


def test_tool_call_completed_失败显示_failed():
    text = _render(
        ToolCallCompleted(
            tool_call=ToolCall(id="c1", name="missing", arguments={}),
            tool_result=ToolExecutionResult(content="not found", is_error=True),
        )
    )

    assert "failed" in text


def test_assistant_message_显示正文与用量_footer():
    text = _render(
        AssistantMessageEvent(
            text="hello world",
            usage=TurnUsage(
                steps=1, input_tokens=100, output_tokens=20,
                elapsed_ms=1500, model_label="anthropic / claude-jupiter-v1-p",
            ),
        )
    )

    assert "hello world" in text
    assert "anthropic / claude-jupiter-v1-p" in text
    assert "100" in text
    assert "20" in text


def test_assistant_message_无用量时不崩():
    text = _render(AssistantMessageEvent(text="hello"))

    assert "hello" in text


def test_turn_级事件不产生输出():
    """E1 阶段 turn_started/completed 只进 trace，不上屏。"""
    assert _render(TurnStarted(user_text="hi")).strip() == ""
    assert _render(TurnCompleted(usage=TurnUsage(steps=1))).strip() == ""
