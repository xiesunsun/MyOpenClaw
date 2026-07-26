from __future__ import annotations

from rich.console import Group, RenderableType
from rich.table import Table
from rich.text import Text

from pickel.runs.measure import ContextUsage
from pickel.runs.turn_usage import TurnUsage


class ContextRenderer:
    """ContextUsage + 真实 API usage 的 `/context` 视图（设计 §7.2）。"""

    BAR_WIDTH = 32

    def render(
        self,
        usage: ContextUsage | None,
        *,
        last_turn: TurnUsage | None = None,
        session_total: TurnUsage | None = None,
        note: str | None = None,
        source_line: str | None = None,
    ) -> RenderableType:
        sections: list[RenderableType] = [Text("Context Usage", style="bold")]
        if note:
            sections.append(Text(note, style="dim"))

        if usage is None:
            sections.append(Text("无法组装上下文（见上）", style="yellow"))
        else:
            sections.append(self._render_header(usage))
            sections.append(self._render_categories(usage))
            catalog = self._find(usage, "skills_catalog")
            if catalog is not None and catalog.details:
                sections.append(self._render_details(catalog))

        if last_turn is not None:
            sections.append(self._render_turn("Last turn", last_turn))
        if session_total is not None:
            sections.append(self._render_turn("Session total", session_total))

        if source_line:
            sections.append(Text(source_line, style="dim"))
        return Group(*sections)

    def _render_header(self, usage: ContextUsage) -> RenderableType:
        return Group(
            Text(usage.model_label, style="cyan"),
            Text(
                f"{self._tokens(usage.total_tokens)} / {self._tokens(usage.max_input_tokens)}"
                f"  {self._percent(usage)}"
            ),
            Text(self._bar(usage)),
            Text(
                "measured（真实 usage 锚）" if usage.is_measured else "estimated（本地估计）",
                style="dim",
            ),
        )

    def _render_categories(self, usage: ContextUsage) -> Table:
        table = Table.grid(padding=(0, 2))
        table.add_row(Text("By category（估计）", style="bold"), Text(""))
        for category in usage.categories:
            table.add_row(Text(category.label), Text(self._tokens(category.tokens)))
        table.add_row(Text("Free space"), Text(self._tokens(usage.free_tokens)))
        return table

    def _render_details(self, category) -> Table:
        table = Table.grid(padding=(0, 2))
        table.add_row(Text("Skills breakdown（估计）", style="bold"), Text(""))
        for detail in category.details:
            table.add_row(Text(detail.label), Text(self._tokens(detail.tokens)))
        return table

    def _render_turn(self, title: str, turn: TurnUsage) -> RenderableType:
        lines: list[RenderableType] = [Text(f"{title}", style="bold")]
        suffix = f"  steps={turn.steps}" if turn.steps else ""
        lines.append(Text(f"  实际输入={turn.actual_input_tokens:,}{suffix}"))
        lines.append(
            Text(
                f"    in={turn.input_tokens:,} "
                f"cache_read={turn.cache_read_tokens:,} "
                f"cache_write={turn.cache_write_tokens:,}"
            )
        )
        lines.append(Text(f"  out={turn.output_tokens:,}"))
        if turn.elapsed_ms:
            lines.append(Text(f"  duration_ms={turn.elapsed_ms:,}"))
        if turn.hook_injected_chars:
            lines.append(Text(f"  hook_injected_chars={turn.hook_injected_chars:,}"))
        if turn.model_label:
            lines.append(Text(f"  model={turn.model_label}"))
        return Group(*lines)

    def _bar(self, usage: ContextUsage) -> str:
        if usage.max_input_tokens in (None, 0):
            return "[unknown]"
        ratio = max(0.0, min(1.0, usage.total_tokens / usage.max_input_tokens))
        filled = round(self.BAR_WIDTH * ratio)
        return f"[{'#' * filled}{'-' * (self.BAR_WIDTH - filled)}]"

    @staticmethod
    def _percent(usage: ContextUsage) -> str:
        if usage.max_input_tokens in (None, 0):
            return ""
        return f"{usage.total_tokens / usage.max_input_tokens * 100:.1f}%"

    @staticmethod
    def _find(usage: ContextUsage, key: str):
        for category in usage.categories:
            if category.key == key:
                return category
        return None

    @staticmethod
    def _tokens(value: int | None) -> str:
        if value is None:
            return "unknown"
        return f"{value:,} tokens"
