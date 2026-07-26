"""流式增量状态机（E3 设计稿 §9.1）。

状态 idle / thinking / text：
- idle→thinking：先打一行 `· 思考中……`（dim），随后增量 dim 输出；
- thinking→text：补换行再输出正常文本；
- idle→text：直接输出（流式只是预览，最终正文由 AssistantMessageEvent 重渲）。

所有增量 highlight=False、markup=False——防增量文本被 rich 解析（E2 决策）。
"""

from __future__ import annotations

from rich.console import Console
from rich.text import Text

_IDLE = "idle"
_THINKING = "thinking"
_TEXT = "text"


class StreamRenderer:
    def __init__(self, console: Console) -> None:
        self.console = console
        self._state = _IDLE

    @property
    def active(self) -> bool:
        return self._state != _IDLE

    def on_thinking(self, text: str) -> None:
        if self._state != _THINKING:
            if self._state == _TEXT:
                self.console.print()
            self.console.print(Text("· 思考中……", style="dim"))
            self._state = _THINKING
        self.console.print(
            text, end="", style="dim", highlight=False, markup=False
        )

    def on_text(self, text: str) -> None:
        if self._state == _THINKING:
            self.console.print()
        self._state = _TEXT
        self.console.print(text, end="", highlight=False, markup=False)

    def end(self) -> None:
        """活跃时补换行并复位；幂等。"""
        if self._state == _IDLE:
            return
        self.console.print()
        self._state = _IDLE
