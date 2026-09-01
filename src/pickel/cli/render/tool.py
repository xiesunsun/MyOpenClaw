"""Tool Runtime Event 的紧凑 CLI 渲染。"""

from __future__ import annotations

from datetime import datetime

from rich.console import Console
from rich.text import Text

from pickel.runtime.runtime_events import ToolCallCompleted, ToolCallStarted

_ARG_VALUE_LIMIT = 120
_ARG_INLINE_LIMIT = 160
_RESULT_MAX_LINES = 5
_RESULT_MAX_CHARS = 400
_PRIORITY_KEYS = ("command", "path", "pattern", "file_path", "query", "text")


class ToolRenderer:
    def __init__(self, console: Console) -> None:
        self.console = console
        self._started_at: dict[str, datetime] = {}

    def on_started(self, event: ToolCallStarted) -> None:
        tool_call_id = event.envelope.identity.tool_call_id or ""
        self._started_at[tool_call_id] = event.envelope.occurred_at
        args = _format_args_inline(event.arguments)
        head = f"⏺ {event.tool_name}" + (f"  {args}" if args else "")
        width = max(20, self.console.width)
        if len(head) > width:
            head = head[: width - 1] + "…"
        self.console.print(head, highlight=False, markup=False)
        self.console.print("· running", highlight=False, markup=False)

    def on_completed(self, event: ToolCallCompleted) -> None:
        tool_call_id = event.envelope.identity.tool_call_id or ""
        started_at = self._started_at.pop(tool_call_id, None)
        elapsed = (
            (event.envelope.occurred_at - started_at).total_seconds()
            if started_at is not None
            else None
        )
        status = Text("· ")
        status.append(
            "failed" if event.is_error else "ok",
            style="red" if event.is_error else "green",
        )
        if elapsed is not None and elapsed >= 0.1:
            status.append(f"  ({elapsed:.1f}s)", style="dim")
        self.console.print(status)
        for line in _result_lines(event.content):
            self.console.print(line, highlight=False, markup=False)


def _result_lines(content: str) -> list[str]:
    if not content:
        return ["· out  (empty)"]
    raw = content.splitlines() or [content]
    shown = raw[:_RESULT_MAX_LINES]
    if sum(len(line) for line in shown) > _RESULT_MAX_CHARS:
        shown = [content[: _RESULT_MAX_CHARS - 1] + "…"]
    lines = [f"· out  {shown[0]}"]
    lines.extend(f"·      {line}" for line in shown[1:])
    if len(raw) > len(shown) or len(content) > _RESULT_MAX_CHARS:
        lines.append(f"·      … / {len(content)} chars")
    return lines


def _format_args_inline(arguments: dict[str, object]) -> str:
    keys = [key for key in _PRIORITY_KEYS if key in arguments]
    keys.extend(sorted(key for key in arguments if key not in keys))
    parts = [_fold_value(key, arguments[key]) for key in keys]
    joined = "  ".join(parts)
    return joined if len(joined) <= _ARG_INLINE_LIMIT else joined[:159] + "…"


def _fold_value(key: str, value: object) -> str:
    if key == "content" or (isinstance(value, str) and len(value) > _ARG_VALUE_LIMIT):
        return f"{key}=<{len(str(value))} chars>"
    rendered = repr(value)
    if len(rendered) > _ARG_VALUE_LIMIT:
        return f"{key}=<{len(str(value))} chars>"
    return f"{key}={rendered}"
