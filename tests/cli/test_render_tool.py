"""render/tool：名与 args 同行，子行 · 左对齐。"""

from __future__ import annotations

import io
from datetime import datetime, timedelta, timezone

from rich.console import Console

from pickel.cli.render.tool import ToolRenderer
from pickel.conversations.message import ToolCall
from pickel.tools.base import ToolExecutionResult

_T0 = datetime(2026, 7, 26, 12, 0, 0, tzinfo=timezone.utc)


def _console(**kwargs) -> Console:
    return Console(width=100, record=True, force_terminal=False, **kwargs)


def _call(call_id: str = "c1", **kwargs) -> ToolCall:
    name = kwargs.pop("name", "echo")
    arguments = kwargs.pop("arguments", {"text": "hi"})
    return ToolCall(id=call_id, name=name, arguments=arguments)


def test_started_名与_args_同一行_running_用点对齐():
    console = _console()
    renderer = ToolRenderer(console)

    renderer.on_started(_call(), _T0)
    lines = console.export_text().splitlines()

    assert lines[0].startswith("⏺ echo")
    assert "text=" in lines[0]
    assert "args" not in lines[0]
    assert lines[1] == "· running"


def test_completed_点号_ok与_out_名只一次():
    console = _console()
    renderer = ToolRenderer(console)

    renderer.on_started(_call(), _T0)
    renderer.on_completed(
        _call(), ToolExecutionResult(content="hi"), _T0 + timedelta(seconds=2.3)
    )

    text = console.export_text()
    assert text.count("⏺ echo") == 1
    assert "· ok" in text
    assert "· out  hi" in text
    assert "(2.3s)" in text
    lines = text.splitlines()
    assert lines[0].startswith("⏺ echo")
    assert lines[1] == "· running"
    assert lines[2].startswith("· ok")
    assert lines[3].startswith("· out")


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
    assert "failed" in text
    assert "boom" in text


def test_间隔不足_100ms_不显示耗时():
    console = _console()
    renderer = ToolRenderer(console)

    renderer.on_started(_call(), _T0)
    renderer.on_completed(
        _call(), ToolExecutionResult(content="hi"), _T0 + timedelta(milliseconds=50)
    )

    ok_line = next(
        line for line in console.export_text().splitlines() if line.startswith("· ok")
    )
    assert "(" not in ok_line


def test_配不上_started_补头再打结果():
    console = _console()
    renderer = ToolRenderer(console)

    renderer.on_completed(
        _call("orphan"), ToolExecutionResult(content="hi"), _T0
    )

    text = console.export_text()
    assert "⏺ echo" in text
    assert "· ok" in text
    assert "hi" in text


def test_空结果标明_empty():
    console = _console()
    renderer = ToolRenderer(console)

    renderer.on_started(_call(), _T0)
    renderer.on_completed(
        _call(), ToolExecutionResult(content=""), _T0 + timedelta(seconds=1)
    )

    assert "· out  (empty)" in console.export_text()


def test_长_content_同行摘要():
    console = _console()
    renderer = ToolRenderer(console)
    body = "line1\n" + ("x" * 200)
    call = _call(arguments={"path": "a.py", "content": body})

    renderer.on_started(call, _T0)
    head = console.export_text().splitlines()[0]

    assert head.startswith("⏺ echo")
    assert "path=" in head
    assert f"content=<{len(body)} chars>" in head


def test_长结果多行折叠():
    console = _console()
    renderer = ToolRenderer(console)
    long_content = "\n".join(f"row{i}" for i in range(20))

    renderer.on_started(_call(), _T0)
    renderer.on_completed(
        _call(), ToolExecutionResult(content=long_content), _T0 + timedelta(seconds=1)
    )

    text = console.export_text()
    assert "· out  row0" in text
    assert "… +" in text


def test_终端模式也只追加不擦屏():
    buffer = io.StringIO()
    console = Console(
        width=100, record=True, force_terminal=True, file=buffer
    )
    renderer = ToolRenderer(console)

    renderer.on_started(_call(), _T0)
    renderer.on_completed(
        _call(), ToolExecutionResult(content="hi"), _T0 + timedelta(seconds=2.3)
    )

    assert "\x1b[1A" not in buffer.getvalue()
    text = console.export_text()
    assert text.count("⏺ echo") == 1
    assert "· ok" in text
