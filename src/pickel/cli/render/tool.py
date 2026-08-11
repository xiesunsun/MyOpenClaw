"""工具块渲染：started 展示调用，completed 追加结果（只追加、不擦屏）。

版式（与 · 思考中 同级左对齐）：
  ⏺ shell_exec  command='date "..."'
  · running
  · ok  (0.2s)
  · out  Mon Jul 27 ...

tool_call_started   → 名与 args 同一行 + · running
tool_call_completed → · ok|failed + · out
"""

from __future__ import annotations

from datetime import datetime

from rich.console import Console
from rich.text import Text

from pickel.runtime.runtime_events import ToolCallSnapshot
from pickel.tools.base import ToolExecutionResult

_DURATION_MIN_SECONDS = 0.1
_ARG_VALUE_LIMIT = 120
_ARG_INLINE_LIMIT = 160
_RESULT_MAX_LINES = 5
_RESULT_MAX_CHARS = 400
_PRIORITY_KEYS = ("command", "path", "pattern", "file_path", "query", "text")


class ToolRenderer:
    def __init__(self, console: Console) -> None:
        self.console = console
        self._started_at: dict[str, datetime] = {}

    def on_started(self, tool_call: ToolCallSnapshot, occurred_at: datetime) -> None:
        self._started_at[tool_call.tool_call_id] = occurred_at
        self._print_plain(self._format_started_block(tool_call))

    def on_completed(
        self,
        tool_call: ToolCallSnapshot,
        tool_result: ToolExecutionResult,
        occurred_at: datetime,
    ) -> None:
        started_at = self._started_at.pop(tool_call.tool_call_id, None)
        elapsed: float | None = None
        if started_at is not None:
            elapsed = (occurred_at - started_at).total_seconds()
            if elapsed < _DURATION_MIN_SECONDS:
                elapsed = None

        if started_at is None:
            self._print_plain(self._format_started_block(tool_call, running=False))
        self._print_status_and_out(tool_call.tool_name, tool_result, elapsed)

    def _print_plain(self, body: str) -> None:
        self.console.print(body, highlight=False, markup=False, end="")

    def _print_status_and_out(
        self,
        tool_name: str,
        tool_result: ToolExecutionResult,
        elapsed: float | None,
    ) -> None:
        status, style, details = _tool_status(tool_name, tool_result)
        head = Text("· ")
        head.append(status, style=style)
        if details:
            head.append(f" · {details}", style="dim")
        if elapsed is not None:
            head.append(f"  ({elapsed:.1f}s)", style="dim")
        self.console.print(head, highlight=False, markup=False)
        out_body = "\n".join(self._format_out_lines(tool_result)) + "\n"
        self._print_plain(out_body)

    def _format_started_block(
        self, tool_call: ToolCallSnapshot, *, running: bool = True
    ) -> str:
        args = _format_args_inline(tool_call.arguments)
        if args:
            head = f"⏺ {tool_call.tool_name}  {args}"
        else:
            head = f"⏺ {tool_call.tool_name}"
        # 超宽时截断到 console 宽，保证单行
        width = max(20, self.console.width)
        if len(head) > width:
            head = head[: width - 1] + "…"
        lines = [head]
        if running:
            lines.append("· running")
        return "\n".join(lines) + "\n"

    def _format_out_lines(self, tool_result: ToolExecutionResult) -> list[str]:
        content = tool_result.content or ""
        if not content:
            return ["· out  (empty)"]
        raw_lines = content.splitlines() or [content]
        total_chars = len(content)
        if len(raw_lines) <= _RESULT_MAX_LINES and total_chars <= _RESULT_MAX_CHARS:
            lines = [f"· out  {raw_lines[0]}"]
            for line in raw_lines[1:]:
                lines.append(f"·      {line}")
            return lines

        first = raw_lines[0]
        if len(first) > _RESULT_MAX_CHARS:
            first = first[: _RESULT_MAX_CHARS - 3] + "..."
        shown = [first] + raw_lines[1:_RESULT_MAX_LINES]
        rest_lines = max(0, len(raw_lines) - _RESULT_MAX_LINES)
        lines = [f"· out  {shown[0]}"]
        for line in shown[1:]:
            lines.append(f"·      {line}")
        if rest_lines > 0:
            lines.append(f"·      … +{rest_lines} lines / {total_chars} chars")
        else:
            lines.append(f"·      … / {total_chars} chars")
        return lines


def _tool_status(
    tool_name: str, tool_result: ToolExecutionResult
) -> tuple[str, str, str]:
    structured = tool_result.structured_content
    if tool_name != "bash" or not isinstance(structured, dict):
        if tool_result.is_error:
            return "failed", "red", ""
        return "ok", "green", ""

    exit_code = structured.get("exit_code")
    shell_status = structured.get("shell_status")
    timed_out = structured.get("timed_out") is True
    if timed_out:
        status, style = "timeout", "yellow"
    elif tool_result.is_error or shell_status == "terminated":
        status, style = "failed", "red"
    elif exit_code == 0:
        status, style = "ok", "green"
    else:
        # 非零退出码是命令执行结果，不是 Runtime 工具调用失败。
        status, style = "completed", "yellow"

    details = []
    if isinstance(exit_code, int):
        details.append(f"exit {exit_code}")
    if isinstance(shell_status, str) and shell_status:
        details.append(shell_status)
    if structured.get("truncated") is True:
        details.append("truncated")
    return status, style, " · ".join(details)


def _format_args_inline(arguments: dict[str, object]) -> str:
    if not arguments:
        return ""
    parts: list[str] = []
    for key, value in _ordered_items(arguments):
        parts.append(_fold_value_inline(key, value))
    joined = "  ".join(parts)
    if len(joined) > _ARG_INLINE_LIMIT:
        return joined[: _ARG_INLINE_LIMIT - 1] + "…"
    return joined


def _ordered_items(arguments: dict[str, object]) -> list[tuple[str, object]]:
    ordered: list[str] = []
    for key in _PRIORITY_KEYS:
        if key in arguments:
            ordered.append(key)
    for key in sorted(k for k in arguments if k not in ordered):
        ordered.append(key)
    return [(key, arguments[key]) for key in ordered]


def _fold_value_inline(key: str, value: object) -> str:
    if key == "content" or (isinstance(value, str) and len(value) > _ARG_VALUE_LIMIT):
        text = value if isinstance(value, str) else str(value)
        return f"{key}=<{len(text)} chars>"
    rendered = repr(value)
    if len(rendered) > _ARG_VALUE_LIMIT:
        return f"{key}=<{len(str(value))} chars>"
    return f"{key}={rendered}"
