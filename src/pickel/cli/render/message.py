"""无边框静态消息渲染（E3 设计稿 §9.1）。

符号前缀 + 缩进分层，不用 Panel。渲染信息只来自调用方传入，
不读 Run/Session。
"""

from __future__ import annotations

from rich.console import Console
from rich.markdown import Markdown
from rich.text import Text

from pickel.runs.turn_usage import TurnUsage


def abbrev_tokens(n: int) -> str:
    """token 数 ≥1000 时以 k 缩写：180 → "180"，2437 → "2.4k"。"""
    if n < 1000:
        return str(n)
    return f"{n / 1000:.1f}k"


def format_footer(
    usage: TurnUsage | None, fallback_model_label: str | None
) -> str | None:
    """footer 单行文本；两者皆空返回 None。

    输入规模一律用 usage.actual_input_tokens（§5.1 口径：
    input + cache_read + cache_write），禁止退回裸 input_tokens。
    """
    if usage is None:
        return fallback_model_label or None

    label = usage.model_label or fallback_model_label
    parts: list[str] = []
    if label:
        parts.append(label)
    parts.append(
        f"{abbrev_tokens(usage.actual_input_tokens)}"
        f"→{abbrev_tokens(usage.output_tokens)}"
    )
    if usage.elapsed_ms:
        parts.append(f"{usage.elapsed_ms / 1000:.1f}s")
    return " · ".join(parts)


def render_header(console: Console, *, agent_id: str, commands_line: str) -> None:
    """无框三行 header：agent、config 路径、命令列表。"""
    console.print(Text(f"Agent: {agent_id}", style="bold cyan"))
    console.print(Text("Config: ~/.pickel + project .pickel / agents", style="dim"))
    console.print(Text(commands_line, style="yellow"))


def render_system(console: Console, text: str, *, style: str = "cyan") -> None:
    console.print(Text(f"· {text}", style=style))


def render_error(console: Console, text: str) -> None:
    console.print(Text(f"✗ {text}", style="red"))


def render_interrupted(console: Console) -> None:
    """「已中断本轮」字样保留——真机 pexpect 脚本按它断言。"""
    console.print(Text("✗ 已中断本轮。", style="yellow"))


def render_assistant(
    console: Console,
    *,
    text: str,
    usage: TurnUsage | None,
    fallback_model_label: str | None,
) -> None:
    console.print(Markdown(text))
    footer = format_footer(usage, fallback_model_label)
    if footer is not None:
        console.print(Text(footer, style="dim", justify="right"))
