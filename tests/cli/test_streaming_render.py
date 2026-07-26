"""流式渲染：delta 增量出字，最终仍渲染完整 Assistant 框。"""

from __future__ import annotations

import asyncio

from rich.console import Console

from pickel.cli.event_renderer import ChatEventRenderer
from pickel.runs.runtime_events import (
    AssistantMessageEvent,
    TextDeltaEvent,
    ThinkingDeltaEvent,
    ToolCallArgsDeltaEvent,
    TurnInterrupted,
)
from pickel.runs.turn_usage import TurnUsage


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


def test_增量之后仍渲染完整_assistant_框():
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

    assert "anthropic / claude-jupiter-v1-p" in text
    assert "in 100 · out 20 · 1.5s" in text


def test_思考增量与正文增量都出现():
    text = _render([ThinkingDeltaEvent(text="想一下"), TextDeltaEvent(text="答案")])

    assert "想一下" in text
    assert "答案" in text


def test_工具参数增量不上屏():
    """partial_json 拼完前不是合法 JSON，展示半截参数只会制造噪音。"""
    text = _render(
        [ToolCallArgsDeltaEvent(tool_call_id="c1", partial_json='{"text"')]
    )

    assert '{"text"' not in text


def test_中断显示提示():
    text = _render([TurnInterrupted(at_step=2, partial_text="写到一半")])

    assert "中断" in text


def test_无_delta_时渲染与改造前一致():
    """非流式 provider 走这条路径，输出不得变化。"""
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
    assert "in 100 · out 20 · 1.5s" in text
