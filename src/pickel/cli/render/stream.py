"""流式增量状态机。

状态 idle / thinking / text：
- idle→thinking：先打一行 `· 思考中……`（dim），随后增量 dim 输出；
- thinking→text：补换行再输出正常文本；
- idle→text：直接输出；
- settle：
  - 已流式打过正文（text_delta）→ 只补 footer（不重打正文）
  - 仅 thinking 或无预览 → 白字正文 + footer（避免正文丢失）

不做 Markdown 定稿、不擦屏。
所有增量 highlight=False、markup=False。
"""

from __future__ import annotations

from rich.console import Console
from rich.text import Text

from pickel.cli.render.message import format_footer, render_assistant
from pickel.runs.turn_usage import TurnUsage

_IDLE = "idle"
_THINKING = "thinking"
_TEXT = "text"


class StreamRenderer:
    def __init__(self, console: Console) -> None:
        self.console = console
        self._state = _IDLE
        # 当前段是否已流式输出过正文（仅 text；thinking 不算）
        self._had_text = False

    @property
    def active(self) -> bool:
        return self._state != _IDLE

    def on_thinking(self, text: str) -> None:
        if self._state != _THINKING:
            if self._state == _TEXT:
                self.console.print()
            self.console.print(Text("· 思考中……", style="dim"))
            self._state = _THINKING
        self.console.print(text, end="", style="dim", highlight=False, markup=False)

    def on_text(self, text: str) -> None:
        if self._state == _THINKING:
            self.console.print()
        self._state = _TEXT
        self.console.print(text, end="", highlight=False, markup=False)
        self._had_text = True

    def end(self) -> None:
        """活跃时补换行并提交预览为历史；幂等。"""
        if self._state == _IDLE:
            return
        self.console.print()
        self._state = _IDLE
        self._had_text = False

    def settle(
        self,
        text: str,
        usage: TurnUsage | None,
        fallback_model_label: str | None,
    ) -> None:
        """流式收尾。

        已流式正文 → 只 footer；否则（含仅 thinking）打白字正文 + footer。
        """
        had_text = self._had_text or self._state == _TEXT
        if self._state != _IDLE:
            self.console.print()
            self._state = _IDLE
        self._had_text = False

        if had_text:
            footer = format_footer(usage, fallback_model_label)
            if footer is not None:
                self.console.print(Text(footer, style="dim"), justify="right")
            return

        render_assistant(
            self.console,
            text=text,
            usage=usage,
            fallback_model_label=fallback_model_label,
        )
