"""render/message：无边框静态消息渲染（E3 Task 1）。"""

from __future__ import annotations

from rich.console import Console

from pickel.cli.render.message import (
    abbrev_tokens,
    format_footer,
    render_assistant,
    render_error,
    render_header,
    render_interrupted,
    render_system,
)
from pickel.runs.turn_usage import TurnUsage


def _console() -> Console:
    return Console(width=100, record=True, force_terminal=False)


# ---- abbrev_tokens ----


def test_abbrev_tokens_千以下原样():
    assert abbrev_tokens(180) == "180"
    assert abbrev_tokens(999) == "999"


def test_abbrev_tokens_千及以上缩写():
    assert abbrev_tokens(1000) == "1.0k"
    assert abbrev_tokens(2437) == "2.4k"


# ---- format_footer ----


def test_footer_输入规模必须是_5_1_口径():
    """§5.1：input + cache_read + cache_write。

    退回裸 input_tokens 的变异会显示 100，本测试必须杀死它。
    """
    usage = TurnUsage(
        steps=1,
        input_tokens=100,
        cache_read_tokens=8000,
        cache_write_tokens=200,
        output_tokens=20,
        elapsed_ms=1500,
        model_label="anthropic / claude-jupiter-v1-p",
    )

    footer = format_footer(usage, None)

    assert footer == "anthropic / claude-jupiter-v1-p · 8.3k→20 · 1.5s"


def test_footer_elapsed_为零省略时间段():
    usage = TurnUsage(
        steps=1, input_tokens=100, output_tokens=20,
        elapsed_ms=0, model_label="anthropic / m",
    )

    assert format_footer(usage, None) == "anthropic / m · 100→20"


def test_footer_model_label_为空退_fallback():
    usage = TurnUsage(steps=1, input_tokens=100, output_tokens=20, elapsed_ms=1000)

    footer = format_footer(usage, "gemini / flash")

    assert footer == "gemini / flash · 100→20 · 1.0s"


def test_footer_usage_为_None_只显示_fallback():
    assert format_footer(None, "anthropic / m") == "anthropic / m"


def test_footer_两者皆空返回_None():
    assert format_footer(None, None) is None
    assert format_footer(None, "") is None


# ---- render_* ----


def test_render_system_带点前缀无边框():
    console = _console()

    render_system(console, "Session closed.")

    text = console.export_text()
    assert "· Session closed." in text
    assert "╭" not in text


def test_render_error_带叉前缀():
    console = _console()

    render_error(console, "Unknown command")

    text = console.export_text()
    assert "✗ Unknown command" in text
    assert "╭" not in text


def test_render_interrupted_保留已中断本轮字样():
    console = _console()

    render_interrupted(console)

    text = console.export_text()
    assert "✗ 已中断本轮。" in text


def test_render_header_三行无框():
    console = _console()

    render_header(
        console,
        agent_id="default",
        commands_line="/help  /exit",
    )

    text = console.export_text()
    assert "Agent: default" in text
    assert "Config:" in text
    assert "/help  /exit" in text
    assert "╭" not in text


def test_render_assistant_白字加_footer_无框_不解析_md():
    console = _console()
    usage = TurnUsage(
        steps=1, input_tokens=100, output_tokens=20,
        elapsed_ms=1500, model_label="anthropic / m",
    )

    render_assistant(console, text="# 标题\n\n正文", usage=usage, fallback_model_label=None)

    text = console.export_text()
    assert "# 标题" in text  # 不解析 Markdown，字面输出
    assert "正文" in text
    assert "anthropic / m · 100→20 · 1.5s" in text
    assert "╭" not in text


def test_render_assistant_footer_为_None_不打_footer():
    console = _console()

    render_assistant(console, text="正文", usage=None, fallback_model_label=None)

    text = console.export_text()
    assert "正文" in text
    assert "·" not in text


def test_render_assistant_usage_None_时_footer_只有_fallback_label():
    console = _console()

    render_assistant(
        console, text="正文", usage=None, fallback_model_label="anthropic / m"
    )

    text = console.export_text()
    assert "anthropic / m" in text
    assert "→" not in text
