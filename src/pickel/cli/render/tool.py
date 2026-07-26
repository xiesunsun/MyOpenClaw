"""工具行渲染：⏺ 行 + 原地 running → ok（E3 设计稿 §9.1/§12）。

不用 rich Live：started 打两行（label 行 + running… 行），completed 时
若 console 是终端则 ANSI 光标上移两行清除重写；非终端（测试 record、
管道）降级为直接追加结果行。

耗时不加 runtime 字段：按 tool_call_id 配对两个信封 occurred_at 相减；
配不上（乱序/丢失 started）就直接打完整两行、不显示耗时。
"""

from __future__ import annotations

from datetime import datetime

from rich.console import Console
from rich.control import Control
from rich.segment import ControlType
from rich.text import Text

from pickel.conversations.message import ToolCall
from pickel.tools.base import ToolExecutionResult

_DURATION_MIN_SECONDS = 0.1


class ToolRenderer:
    def __init__(self, console: Console) -> None:
        self.console = console
        self._started: dict[str, tuple[datetime, Text]] = {}

    def on_started(self, tool_call: ToolCall, occurred_at: datetime) -> None:
        label = self._label_line(tool_call)
        self._started[tool_call.id] = (occurred_at, label)
        self.console.print(label)
        self.console.print(Text("  running…", style="dim"))

    def on_completed(
        self,
        tool_call: ToolCall,
        tool_result: ToolExecutionResult,
        occurred_at: datetime,
    ) -> None:
        record = self._started.pop(tool_call.id, None)
        if record is None:
            # 乱序/丢失 started：直接打完整两行，不显示耗时
            self.console.print(self._label_line(tool_call))
            self.console.print(self._status_line(tool_result, elapsed=None))
            return

        started_at, label = record
        elapsed = (occurred_at - started_at).total_seconds()
        status = self._status_line(
            tool_result,
            elapsed=elapsed if elapsed >= _DURATION_MIN_SECONDS else None,
        )

        if self.console.is_terminal:
            # 光标上移两行，清掉 label 行重打，再清掉 running… 行打状态行
            self.console.control(
                Control((ControlType.CURSOR_UP, 2), (ControlType.ERASE_IN_LINE, 2))
            )
            self.console.print(label)
            self.console.control(Control((ControlType.ERASE_IN_LINE, 2)))
            self.console.print(status)
        else:
            self.console.print(status)

    def _label_line(self, tool_call: ToolCall) -> Text:
        """`⏺ name  args摘要`，整行截到 console 宽度内保证单行。"""
        summary = self._format_args(tool_call.arguments)
        raw = f"⏺ {tool_call.name}  {summary}" if summary else f"⏺ {tool_call.name}"
        label = Text(raw, no_wrap=True)
        label.truncate(self.console.width, overflow="ellipsis")
        return label

    @staticmethod
    def _status_line(tool_result: ToolExecutionResult, *, elapsed: float | None) -> Text:
        status = "failed" if tool_result.is_error else "ok"
        line = Text("  ")
        line.append(status, style="red" if tool_result.is_error else "green")
        summary = _truncate_content(tool_result.content) if tool_result.content else ""
        if summary:
            line.append(f" · {summary}")
        if elapsed is not None:
            line.append(f" ({elapsed:.1f}s)", style="dim")
        return line

    @staticmethod
    def _format_args(arguments: dict[str, object]) -> str:
        """args 摘要；截断规则沿用 event_renderer._format_tool_label。"""
        parts: list[str] = []
        for key, value in arguments.items():
            rendered = repr(value)
            if key == "content":
                rendered = f"<{len(str(value))} chars>"
            elif len(rendered) > 100:
                rendered = f"{rendered[:97]}..."
            parts.append(f"{key}={rendered}")
        return ", ".join(parts)


def _truncate_content(content: str, limit: int = 180) -> str:
    """结果摘要；规则沿用 event_renderer._truncate_content。"""
    normalized = " ".join(content.split())
    if len(normalized) <= limit:
        return normalized
    return f"{normalized[:limit - 3]}..."
