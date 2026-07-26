"""流式渲染：delta 增量出字，最终渲染无边框正文与 footer。"""

from __future__ import annotations

import asyncio

import pytest
from rich.console import Console

from pickel.cli.event_renderer import ChatEventRenderer
from pickel.conversations.message import ToolCall
from pickel.runs.runtime_events import (
    AssistantMessageEvent,
    StepStarted,
    TextDeltaEvent,
    ThinkingDeltaEvent,
    ToolCallArgsDeltaEvent,
    ToolCallStarted,
    TurnInterrupted,
)
from pickel.runs.turn_usage import TurnUsage
from pickel.shared.event_envelope import EventEnvelope


def _render(events) -> str:
    console = Console(width=100, record=True, force_terminal=False)
    renderer = ChatEventRenderer(console)
    for event in events:
        asyncio.run(renderer.handle_event(event))
    return console.export_text()


def test_文本增量按顺序出现():
    text = _render(
        [TextDeltaEvent(text="你"), TextDeltaEvent(text="好"), TextDeltaEvent(text="呀")]
    )

    assert "你好呀" in text


def test_增量之后仍渲染完整_assistant_正文():
    text = _render(
        [
            TextDeltaEvent(text="你好"),
            AssistantMessageEvent(
                text="你好",
                usage=TurnUsage(
                    steps=1, input_tokens=100, output_tokens=20,
                    elapsed_ms=1500, model_label="anthropic / claude-jupiter-v1-p",
                ),
            ),
        ]
    )

    assert "anthropic / claude-jupiter-v1-p · 100→20 · 1.5s" in text
    assert "╭" not in text


def test_思考增量与正文增量都出现():
    text = _render([ThinkingDeltaEvent(text="想一下"), TextDeltaEvent(text="答案")])

    assert "思考中" in text
    assert "想一下" in text
    assert "答案" in text


def test_工具参数增量不上屏():
    """partial_json 拼完前不是合法 JSON，展示半截参数只会制造噪音。"""
    text = _render(
        [ToolCallArgsDeltaEvent(tool_call_id="c1", partial_json='{"text"')]
    )

    assert '{"text"' not in text


@pytest.mark.parametrize(
    ("event", "next_line_marker"),
    [
        (AssistantMessageEvent(text="最终回复"), "最终回复"),
        (StepStarted(envelope=EventEnvelope(step_index=1)), None),
        (
            ToolCallStarted(
                envelope=EventEnvelope(step_index=1),
                batch_id="batch-1",
                call_index=0,
                total_calls=1,
                tool_call=ToolCall(id="c1", name="echo", arguments={"text": "hi"}),
            ),
            "⏺ echo",
        ),
    ],
    ids=["assistant_message", "step_started", "tool_call_started"],
)
def test_渲染事件前流式输出必须收尾换行(event, next_line_marker):
    """E2 守护的等价新口径：任一渲染事件到来前必须收尾流式行
    （stream.end()），否则正文/工具行粘在流式文字同一行：
    `流式预览⏺ echo …` 或 `流式预览最终回复`。"""
    text = _render([TextDeltaEvent(text="流式预览"), event])

    lines = text.splitlines()
    idx = next(i for i, line in enumerate(lines) if "流式预览" in line)
    # 流式文字行独占一行：不含 ⏺，正文/工具行另起行
    assert lines[idx].strip() == "流式预览"
    assert "⏺" not in lines[idx]
    if next_line_marker is None:
        # StepStarted 不再上屏，但仍应 stream.end()：其后无粘行
        assert text == "流式预览\n"
    else:
        assert any(next_line_marker in line for line in lines[idx + 1 :])


def test_中断显示提示():
    text = _render([TurnInterrupted(at_step=2, partial_text="写到一半")])

    assert "已中断本轮" in text


def test_无_delta_时渲染正文与_footer():
    """非流式 provider 走这条路径：正文 Markdown 无框 + 单行 footer。"""
    text = _render(
        [
            AssistantMessageEvent(
                text="完整回复",
                usage=TurnUsage(
                    steps=1, input_tokens=100, output_tokens=20,
                    elapsed_ms=1500, model_label="anthropic / claude-jupiter-v1-p",
                ),
            )
        ]
    )

    assert "完整回复" in text
    assert "anthropic / claude-jupiter-v1-p · 100→20 · 1.5s" in text
    assert "╭" not in text
