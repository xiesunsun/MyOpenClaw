"""流式增量状态机。

状态 idle / thinking / text：
- idle→thinking：先打一行 `· 思考中……`（dim），随后增量 dim 输出；
- thinking→text：补换行再输出正常文本；
- idle→text：直接输出；
- settle：有预览则只补 footer（不重打正文）；无预览则白字正文 + footer。

不做 Markdown 定稿、不擦屏——避免双份与行账问题。
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
        # 当前未提交预览是否已输出过（end 会提交并清零）
        self._had_preview = False

    @property
    def active(self) -> bool:
        return self._state != _IDLE

    def on_thinking(self, text: str) -> None:
        if self._state != _THINKING:
            if self._state == _TEXT:
                self.console.print()
            self.console.print(Text("· 思考中……", style="dim"))
            self._state = _THINKING
            self._had_preview = True
        self.console.print(
            text, end="", style="dim", highlight=False, markup=False
        )
        self._had_preview = True

    def on_text(self, text: str) -> None:
        if self._state == _THINKING:
            self.console.print()
        self._state = _TEXT
        self.console.print(text, end="", highlight=False, markup=False)
        self._had_preview = True

    def end(self) -> None:
        """活跃时补换行并提交预览为历史；幂等。"""
        if self._state == _IDLE:
            return
        self.console.print()
        self._state = _IDLE
        self._had_preview = False

    def settle(
        self,
        text: str,
        usage: TurnUsage | None,
        fallback_model_label: str | None,
    ) -> None:
        """流式收尾：有预览只打 footer；无预览打白字正文 + footer。"""
        had_preview = self._had_preview or self._state != _IDLE
        if self._state != _IDLE:
            self.console.print()
            self._state = _IDLE
        self._had_preview = False

        if had_preview:
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
