"""事件渲染：分派器把事件转成无边框排版（E3），不再画 Panel。"""

from __future__ import annotations

import asyncio

from rich.console import Console

from pickel.cli.event_renderer import ChatEventRenderer
from pickel.runs.tool_call import ToolCall
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


def _cached_usage() -> TurnUsage:
    return TurnUsage(
        steps=2,
        input_tokens=100,
        cache_read_tokens=8200,
        output_tokens=20,
        elapsed_ms=1500,
        model_label="anthropic / claude-jupiter-v1-p",
    )


def test_step_started_不再上屏():
    """无边框排版下 `Step N` 行是噪音（E3 分派表：StepStarted 只收尾流式行）。"""
    text = _render(StepStarted(envelope=EventEnvelope(step_index=2)))

    assert text.strip() == ""


def test_tool_call_started_显示_tool_行与_running():
    text = _render(
        ToolCallStarted(
            tool_call=ToolCall(id="c1", name="echo", arguments={"text": "hi"}),
            batch_id="b1", call_index=0, total_calls=1,
        )
    )

    assert "⏺ echo" in text
    assert "text='hi'" in text
    assert "running" in text
    assert "╭" not in text


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
    assert "100→20" in text
    assert "╭" not in text


def test_footer_用量口径是实际输入而非裸_input_tokens():
    """O1 §5.1：in = input + cache_read + cache_write。

    上面那条用例 cache_read=0，`input_tokens` 与 `actual_input_tokens` 恰好相等，
    分辨不出读的是哪个字段；这里让两者不同。
    """
    text = _render(AssistantMessageEvent(text="hi", usage=_cached_usage()))

    assert "8.3k→20" in text
    assert "100→20" not in text


def test_footer_格式逐字锁定():
    """单行右对齐，固定显示 cache read/write，包括零值。"""
    text = _render(AssistantMessageEvent(text="hi", usage=_cached_usage()))

    last_line = [line for line in text.splitlines() if line.strip()][-1]
    assert last_line.strip() == (
        "anthropic / claude-jupiter-v1-p · 8.3k→20"
        " · cache r8.2k/w0 · 1.5s"
    )
    assert last_line.startswith(" ")  # 右对齐


def test_assistant_message_无用量时不崩():
    text = _render(AssistantMessageEvent(text="hello"))

    assert "hello" in text


def test_assistant_message_无用量时_footer_退到_fallback_label():
    """E2 遗留修复：usage=None 时 footer 只显示注入的 fallback label。"""
    console = Console(width=100, record=True, force_terminal=False)
    renderer = ChatEventRenderer(
        console, fallback_model_label="google/gemini / gemini-3-flash-preview"
    )
    asyncio.run(renderer.handle_event(AssistantMessageEvent(text="hello")))
    text = console.export_text()

    assert "hello" in text
    assert "google/gemini / gemini-3-flash-preview" in text


def test_turn_级事件不产生输出():
    """turn_started/completed 只进 trace，不上屏。"""
    assert _render(TurnStarted(user_text="hi")).strip() == ""
    assert _render(TurnCompleted(usage=TurnUsage(steps=1))).strip() == ""
