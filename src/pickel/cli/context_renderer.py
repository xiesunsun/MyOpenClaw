from __future__ import annotations

from rich.console import Group, RenderableType
from rich.table import Table
from rich.text import Text

from pickel.runs.context_usage import ContextUsageCategory, ContextUsageSnapshot


class ContextRenderer:
    BAR_WIDTH = 32

    def render(self, snapshot: ContextUsageSnapshot) -> RenderableType:
        sections: list[RenderableType] = [
            Text("Context Usage", style="bold"),
            self._render_usage_header(snapshot),
            self._render_category_summary(snapshot),
        ]

        skills = snapshot.category("skills")
        if skills.details:
            sections.append(self._render_skills_breakdown(skills))

        return Group(*sections)

    def _render_usage_header(self, snapshot: ContextUsageSnapshot) -> RenderableType:
        used = self._format_token_count(snapshot.total_tokens)
        maximum = self._format_token_count(snapshot.max_input_tokens)
        return Group(
            Text(snapshot.model_label, style="cyan"),
            Text(f"{used} / {maximum}"),
            Text(self._render_bar(snapshot)),
        )

    def _render_category_summary(self, snapshot: ContextUsageSnapshot) -> Table:
        table = Table.grid(padding=(0, 2))
        table.add_row(Text("Estimated usage by category", style="bold"), Text(""))
        for category in snapshot.categories:
            table.add_row(
                Text(category.label),
                Text(self._format_category_usage(category)),
            )
        table.add_row(
            Text("Free space"),
            Text(self._format_token_count(snapshot.free_tokens)),
        )
        return table

    def _render_skills_breakdown(self, category: ContextUsageCategory) -> Table:
        table = Table.grid(padding=(0, 2))
        table.add_row(Text("Skills breakdown", style="bold"), Text(""))
        for detail in category.details:
            table.add_row(
                Text(detail.label),
                Text(self._format_token_count(detail.token_count)),
            )
        return table

    def _render_bar(self, snapshot: ContextUsageSnapshot) -> str:
        if snapshot.total_tokens is None or snapshot.max_input_tokens in (None, 0):
            return "[unknown]"
        ratio = max(0.0, min(1.0, snapshot.total_tokens / snapshot.max_input_tokens))
        filled = round(self.BAR_WIDTH * ratio)
        return f"[{'#' * filled}{'-' * (self.BAR_WIDTH - filled)}]"

    @staticmethod
    def _format_token_count(value: int | None) -> str:
        if value is None:
            return "unknown"
        return f"{value:,} tokens"

    def _format_category_usage(self, category: ContextUsageCategory) -> str:
        if category.char_count is not None and category.token_count is None:
            return f"{category.char_count:,} chars"
        return self._format_token_count(category.token_count)


class ModelContextRenderer:
    """展示 final/predicted ModelContext 与 cache usage。"""

    def render_observation(self, observation) -> RenderableType:
        from pickel.context.observation import ContextObservation

        lines: list[RenderableType] = [Text("ModelContext", style="bold")]
        if observation.predicted:
            lines.append(Text("predicted=true（尚无实际 ModelContext 或仅预测）", style="yellow"))
        if observation.note:
            lines.append(Text(observation.note))
        ctx = observation.model_context
        if ctx is None:
            lines.append(Text("无 ModelContext"))
            return Group(*lines)
        try:
            lines.append(Text(f"system_sections={len(ctx.system.sections)}"))
            lines.append(Text(f"messages={len(ctx.messages)}"))
            lines.append(Text(f"tools={len(ctx.tools)}"))
        except Exception:
            lines.append(Text("model_context present"))
        meta = observation.assistant_metadata
        if meta and meta.usage:
            u = meta.usage
            lines.append(
                Text(
                    "usage: "
                    f"in={u.input_tokens} out={u.output_tokens} "
                    f"cache_read={u.cache_read_tokens} cache_write={u.cache_write_tokens}"
                )
            )
        return Group(*lines)
