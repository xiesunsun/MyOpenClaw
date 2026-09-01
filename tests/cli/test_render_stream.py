"""render/stream：流式增量状态机（E3 Task 2）。"""

from __future__ import annotations

from rich.console import Console

from pickel.cli.render.stream import StreamRenderer


def _make() -> tuple[Console, StreamRenderer]:
    console = Console(width=100, record=True, force_terminal=False)
    return console, StreamRenderer(console)


def test_idle_到_thinking_先打思考中头行():
    console, renderer = _make()

    renderer.on_thinking("想一")
    renderer.on_thinking("下")

    text = console.export_text()
    lines = text.splitlines()
    assert lines[0] == "· 思考中……"
    assert "想一下" in lines[1]


def test_thinking_到_text_切换补换行():
    console, renderer = _make()

    renderer.on_thinking("想一下")
    renderer.on_text("答案")

    text = console.export_text()
    lines = text.splitlines()
    assert lines[0] == "· 思考中……"
    assert lines[1] == "想一下"
    assert lines[2] == "答案"


def test_idle_到_text_直接输出无思考头():
    console, renderer = _make()

    renderer.on_text("你")
    renderer.on_text("好")

    text = console.export_text()
    assert "你好" in text
    assert "思考中" not in text


def test_markup_false_守护_增量含标记字面输出():
    console, renderer = _make()

    renderer.on_text("[red]危险[/red]")

    assert "[red]危险[/red]" in console.export_text()


def test_thinking_增量同样不解析标记():
    console, renderer = _make()

    renderer.on_thinking("[bold]x[/bold]")

    assert "[bold]x[/bold]" in console.export_text()


def test_end_活跃时补换行并复位():
    console, renderer = _make()

    renderer.on_text("hi")
    assert renderer.active
    renderer.end()

    assert not renderer.active
    assert console.export_text() == "hi\n"


def test_end_幂等_调两次只出一个换行():
    console, renderer = _make()

    renderer.on_text("hi")
    renderer.end()
    renderer.end()

    assert console.export_text() == "hi\n"


def test_idle_时_end_无输出():
    console, renderer = _make()

    renderer.end()

    assert console.export_text() == ""
    assert not renderer.active


def test_end_后再来增量重新开一段():
    console, renderer = _make()

    renderer.on_thinking("想")
    renderer.end()
    renderer.on_thinking("再想")

    text = console.export_text()
    assert text.count("· 思考中……") == 2


def test_settle_同文预览只补_footer():
    from pickel.runtime.agent_run_usage import AgentRunUsage

    console, renderer = _make()
    renderer.on_text("你好")
    renderer.settle(
        "你好",
        AgentRunUsage(
            steps=1,
            input_tokens=10,
            output_tokens=2,
            elapsed_ms=1000,
            model_label="m",
        ),
        None,
    )
    text = console.export_text()
    assert text.count("你好") == 1
    assert "m · 10→2 · cache r0/w0 · 1.0s" in text


def test_settle_仅_thinking_仍打印最终正文():
    """thinking 不算正文预览；定稿 text 必须上屏。"""
    from pickel.runtime.agent_run_usage import AgentRunUsage

    console, renderer = _make()
    renderer.on_thinking("内部推理")
    renderer.settle(
        "给用户的回复",
        AgentRunUsage(steps=1, input_tokens=1, output_tokens=1, model_label="m"),
        None,
    )
    text = console.export_text()
    assert "内部推理" in text
    assert "给用户的回复" in text
    assert "m ·" in text


def test_end_后预览不参与_settle_需重打正文():
    """中间 step 已 end：settle 视为无当前预览，白字打定稿 + footer。"""
    from pickel.runtime.agent_run_usage import AgentRunUsage

    console, renderer = _make()
    renderer.on_text("中间话")
    renderer.end()
    renderer.settle(
        "最终回复",
        AgentRunUsage(steps=1, input_tokens=1, output_tokens=1, model_label="m"),
        None,
    )
    text = console.export_text()
    assert "中间话" in text
    assert "最终回复" in text
