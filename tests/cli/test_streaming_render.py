"""流式渲染：delta 增量出字，最终渲染无边框正文与 footer。"""

from __future__ import annotations

import asyncio

import pytest
from rich.console import Console

from pickel.cli.event_renderer import ChatEventRenderer
from pickel.runtime.runtime_events import ToolCallSnapshot
from pickel.runtime.runtime_events import (
    AssistantMessageEvent,
    ModelStepStarted,
    TextDeltaEvent,
    ThinkingDeltaEvent,
    ToolCallArgsDeltaEvent,
    ToolCallStarted,
    AgentRunInterrupted,
)
from pickel.runtime.agent_run_usage import AgentRunUsage
from pickel.shared.event_envelope import EventEnvelope
from pickel.shared.execution_identity import ExecutionIdentity


def _render(events) -> str:
    console = Console(width=100, record=True, force_terminal=False)
    renderer = ChatEventRenderer(console)
    for event in events:
        asyncio.run(renderer.handle_event(event))
    return console.export_text()


def test_文本增量按顺序出现():
    text = _render(
        [
            TextDeltaEvent(text="你"),
            TextDeltaEvent(text="好"),
            TextDeltaEvent(text="呀"),
        ]
    )

    assert "你好呀" in text


def test_settle_后正文一份且有_footer():
    """同文预览 + 定稿：非终端只补 footer，正文不双份。"""
    text = _render(
        [
            TextDeltaEvent(text="你好"),
            AssistantMessageEvent(
                text="你好",
                usage=AgentRunUsage(
                    steps=1,
                    input_tokens=100,
                    output_tokens=20,
                    elapsed_ms=1500,
                    model_label="anthropic / claude-jupiter-v1-p",
                ),
            ),
        ]
    )

    assert text.count("你好") == 1
    assert ("anthropic / claude-jupiter-v1-p · 100→20" " · cache r0/w0 · 1.5s") in text
    assert "╭" not in text


def test_思考增量与正文增量都出现():
    text = _render([ThinkingDeltaEvent(text="想一下"), TextDeltaEvent(text="答案")])

    assert "思考中" in text
    assert "想一下" in text
    assert "答案" in text


def test_工具参数增量不上屏():
    """partial_json 拼完前不是合法 JSON，展示半截参数只会制造噪音。"""
    text = _render(
        [
            ToolCallArgsDeltaEvent(
                envelope=EventEnvelope(identity=ExecutionIdentity(tool_call_id="c1")),
                partial_json='{"text"',
            )
        ]
    )

    assert '{"text"' not in text


@pytest.mark.parametrize(
    ("event", "next_line_marker"),
    [
        # 有流式预览时 settle 只补 footer，不再重打正文
        (AssistantMessageEvent(text="最终回复"), None),
        (
            ModelStepStarted(
                envelope=EventEnvelope(identity=ExecutionIdentity(step_sequence=1))
            ),
            None,
        ),
        (
            ToolCallStarted(
                envelope=EventEnvelope(identity=ExecutionIdentity(step_sequence=1)),
                batch_id="batch-1",
                call_index=0,
                total_calls=1,
                tool_call=ToolCallSnapshot(
                    tool_call_id="c1", tool_name="echo", arguments={"text": "hi"}
                ),
            ),
            "⏺ echo",
        ),
    ],
    ids=["assistant_message", "step_started", "tool_call_started"],
)
def test_渲染事件前流式输出必须收尾换行(event, next_line_marker):
    """流式行须先收尾换行，不得与后续工具行粘在同一行。"""
    text = _render([TextDeltaEvent(text="流式预览"), event])

    lines = text.splitlines()
    idx = next(i for i, line in enumerate(lines) if "流式预览" in line)
    assert lines[idx].strip() == "流式预览"
    assert "⏺" not in lines[idx]
    if next_line_marker is None:
        assert "最终回复" not in text or isinstance(event, ModelStepStarted)
        # 流式正文只一份
        assert text.count("流式预览") == 1
    else:
        assert any(next_line_marker in line for line in lines[idx + 1 :])


def test_中断显示提示():
    text = _render([AgentRunInterrupted(at_step=2, partial_text="写到一半")])

    assert "已中断本轮" in text


def test_无_delta_时渲染正文与_footer():
    """非流式 provider：白字正文 + 单行 footer。"""
    text = _render(
        [
            AssistantMessageEvent(
                text="完整回复",
                usage=AgentRunUsage(
                    steps=1,
                    input_tokens=100,
                    output_tokens=20,
                    elapsed_ms=1500,
                    model_label="anthropic / claude-jupiter-v1-p",
                ),
            )
        ]
    )

    assert "完整回复" in text
    assert ("anthropic / claude-jupiter-v1-p · 100→20" " · cache r0/w0 · 1.5s") in text
    assert "╭" not in text
