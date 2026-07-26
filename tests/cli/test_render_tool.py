"""render/tool：工具行原地更新（E3 Task 3）。"""

from __future__ import annotations

import io
from datetime import datetime, timedelta, timezone

from rich.console import Console

from pickel.cli.render.tool import ToolRenderer
from pickel.conversations.message import ToolCall
from pickel.tools.base import ToolExecutionResult

_T0 = datetime(2026, 7, 26, 12, 0, 0, tzinfo=timezone.utc)


def _console() -> Console:
    return Console(width=100, record=True, force_terminal=False)


def _call(call_id: str = "c1") -> ToolCall:
    return ToolCall(id=call_id, name="echo", arguments={"text": "hi"})


def test_非终端模式_running_与_ok_行都在且顺序正确():
    console = _console()
    renderer = ToolRenderer(console)

    renderer.on_started(_call(), _T0)
    renderer.on_completed(
        _call(), ToolExecutionResult(content="hi"), _T0 + timedelta(seconds=2.3)
    )

    lines = console.export_text().splitlines()
    label_idx = next(i for i, line in enumerate(lines) if "⏺ echo" in line)
    running_idx = next(i for i, line in enumerate(lines) if "running…" in line)
    ok_idx = next(i for i, line in enumerate(lines) if "ok · hi" in line)
    assert label_idx < running_idx < ok_idx


def test_结果行缩进两格():
    console = _console()
    renderer = ToolRenderer(console)

    renderer.on_started(_call(), _T0)
    renderer.on_completed(
        _call(), ToolExecutionResult(content="hi"), _T0 + timedelta(seconds=2.3)
    )

    ok_line = next(
        line for line in console.export_text().splitlines() if "ok · hi" in line
    )
    assert ok_line.startswith("  ok")


def test_failed_分支():
    console = _console()
    renderer = ToolRenderer(console)

    renderer.on_started(_call(), _T0)
    renderer.on_completed(
        _call(),
        ToolExecutionResult(content="boom", is_error=True),
        _T0 + timedelta(seconds=1),
    )

    text = console.export_text()
    assert "failed · boom" in text


def test_耗时配对显示():
    console = _console()
    renderer = ToolRenderer(console)

    renderer.on_started(_call(), _T0)
    renderer.on_completed(
        _call(), ToolExecutionResult(content="hi"), _T0 + timedelta(seconds=2.3)
    )

    assert "(2.3s)" in console.export_text()


def test_间隔不足_100ms_不显示耗时():
    console = _console()
    renderer = ToolRenderer(console)

    renderer.on_started(_call(), _T0)
    renderer.on_completed(
        _call(), ToolExecutionResult(content="hi"), _T0 + timedelta(milliseconds=50)
    )

    ok_line = next(
        line for line in console.export_text().splitlines() if "ok · hi" in line
    )
    assert "(" not in ok_line


def test_配不上_started_直接打完整两行不炸():
    console = _console()
    renderer = ToolRenderer(console)

    renderer.on_completed(
        _call("orphan"), ToolExecutionResult(content="hi"), _T0
    )

    text = console.export_text()
    assert "⏺ echo" in text
    assert "ok · hi" in text
    # 没有 started 就没有耗时
    assert "s)" not in text


def test_结果摘要沿用截断规则_压缩空白并截到_180():
    console = _console()
    renderer = ToolRenderer(console)
    long_content = "word  \n\t spaced " + "x" * 500

    renderer.on_started(_call(), _T0)
    renderer.on_completed(
        _call(), ToolExecutionResult(content=long_content), _T0 + timedelta(seconds=1)
    )

    text = console.export_text()
    assert "word spaced" in text
    assert "..." in text
    assert "x" * 200 not in text


def test_工具行截断到宽度内保证单行():
    console = Console(width=40, record=True, force_terminal=False)
    renderer = ToolRenderer(console)
    call = ToolCall(
        id="c1", name="echo", arguments={"text": "a" * 200}
    )

    renderer.on_started(call, _T0)

    lines = console.export_text().splitlines()
    label_lines = [line for line in lines if "⏺" in line]
    assert len(label_lines) == 1
    assert len(label_lines[0]) <= 40


def test_终端模式_ANSI_原地更新且导出文本含结果行():
    buffer = io.StringIO()
    console = Console(
        width=100, record=True, force_terminal=True, file=buffer
    )
    renderer = ToolRenderer(console)

    renderer.on_started(_call(), _T0)
    renderer.on_completed(
        _call(), ToolExecutionResult(content="hi"), _T0 + timedelta(seconds=2.3)
    )

    raw = buffer.getvalue()
    assert "\x1b[2A" in raw  # 光标上移两行
    text = console.export_text()
    assert "ok · hi" in text
    assert "(2.3s)" in text


def test_终端模式_配不上_started_不发_ANSI():
    buffer = io.StringIO()
    console = Console(
        width=100, record=True, force_terminal=True, file=buffer
    )
    renderer = ToolRenderer(console)

    renderer.on_completed(_call("orphan"), ToolExecutionResult(content="hi"), _T0)

    assert "\x1b[2A" not in buffer.getvalue()
    assert "ok · hi" in console.export_text()
