"""`/context` 视图：对齐简洁 Context 面板（占用图 + 分栏）。

占用图：100 格（5×20）
  ◆ 实心 = 已占用（柱形感）
  ◇ 空心 = 尚未占用（剩余上下文）
比例 = total_tokens / max_input_tokens；remaining 来自 free_tokens（真数据）。
"""

from __future__ import annotations

from rich.console import Group, RenderableType
from rich.text import Text

from pickel.cli.render.message import abbrev_tokens
from pickel.runs.measure import ContextCategory, ContextUsage
from pickel.runs.turn_usage import TurnUsage

# 主占用图：5×20 = 100 格
_GRID_ROWS = 5
_GRID_COLS = 20
_GRID_CELLS = _GRID_ROWS * _GRID_COLS

_SOURCE_LABELS = {
    "anchor": "measured · 真实 usage 锚",
    "anchor_plus_tail": "measured+est · 锚 + 尾部估计",
    "counted": "counted · provider 计数",
    "estimated": "estimated · 本地估计",
}


class ContextRenderer:
    """ContextUsage + 会话统计的 `/context` 视图。"""

    def render(
        self,
        usage: ContextUsage | None,
        *,
        last_turn: TurnUsage | None = None,
        session_total: TurnUsage | None = None,
        note: str | None = None,
        source_line: str | None = None,
        turns: int = 0,
        tool_calls: int = 0,
        compactions: int = 0,
        tool_definitions: int = 0,
    ) -> RenderableType:
        sections: list[RenderableType] = [Text("Context", style="bold")]
        if note:
            sections.append(Text(note, style="dim"))

        if usage is None:
            sections.append(Text("无法组装上下文（见上）", style="yellow"))
        else:
            sections.extend(
                self._render_usage_block(usage, tool_definitions=tool_definitions)
            )
            sections.append(Text(""))
            sections.append(
                Text(
                    f"Turns: {turns} · Tool calls: {tool_calls}"
                    f" · Compactions: {compactions}",
                    style="dim",
                )
            )

        if last_turn is not None:
            sections.append(Text(""))
            sections.append(self._render_turn("Last turn", last_turn))
        if session_total is not None:
            sections.append(self._render_turn("Session total", session_total))

        if source_line:
            sections.append(Text(source_line, style="dim"))
        return Group(*sections)

    def _render_usage_block(
        self, usage: ContextUsage, *, tool_definitions: int = 0
    ) -> list[RenderableType]:
        lines: list[RenderableType] = []
        lines.append(Text(self._header_line(usage), style="bold"))
        lines.append(Text(usage.model_label, style="cyan"))
        lines.append(Text(""))
        lines.append(Text(self._diamond_grid(usage)))
        lines.append(Text(""))

        by_key = {category.key: category for category in usage.categories}
        system = by_key.get("behavior")
        messages = by_key.get("messages")
        tools = by_key.get("tools")
        skills_tokens = _sum_keys(by_key, "skills_guidance", "skills_catalog")
        skills_count = _detail_count(by_key.get("skills_catalog"))
        other = by_key.get("other")

        max_tokens = usage.max_input_tokens or 0
        lines.append(
            self._row("◆", "System prompt", system.tokens if system else 0, max_tokens)
        )
        lines.append(
            self._row("◆", "Messages", messages.tokens if messages else 0, max_tokens)
        )
        free = usage.free_tokens if usage.free_tokens is not None else 0
        lines.append(self._row("◇", "Free", free, max_tokens, style="dim"))
        lines.append(Text(""))

        tool_extra = f" · {tool_definitions} tools" if tool_definitions else ""
        lines.append(
            self._row(
                "◈",
                "Tool definitions",
                tools.tokens if tools else 0,
                max_tokens,
                extra=tool_extra,
            )
        )
        skill_extra = f" · {skills_count} skills" if skills_count else ""
        lines.append(
            self._row(
                "◈",
                "Skills",
                skills_tokens,
                max_tokens,
                extra=skill_extra,
            )
        )
        if other is not None and other.tokens > 0:
            lines.append(self._row("◈", "Other", other.tokens, max_tokens))

        lines.append(Text(""))
        source = _SOURCE_LABELS.get(usage.total_source, "estimated · 本地估计")
        lines.append(Text(source, style="dim"))
        # remaining = 窗口内尚未占用的 token（与 ◇ Free / 空心格同一口径）
        if usage.free_tokens is not None and usage.max_input_tokens not in (None, 0):
            lines.append(
                Text(
                    f"Remaining · ~{abbrev_tokens(usage.free_tokens)} tokens free",
                    style="dim",
                )
            )
        return lines

    def _header_line(self, usage: ContextUsage) -> str:
        used = abbrev_tokens(usage.total_tokens)
        if usage.max_input_tokens in (None, 0):
            return f"{used} tokens"
        cap = abbrev_tokens(usage.max_input_tokens)
        pct = usage.total_tokens / usage.max_input_tokens * 100
        return f"{used} / {cap} tokens ({pct:.2f}%)"

    def _diamond_grid(self, usage: ContextUsage) -> str:
        """实心 ◆ = 已占用；空心 ◇ = 未占用。按 total/max 比例填格。"""
        filled = _occupied_cells(usage.total_tokens, usage.max_input_tokens)
        cells = ["◆" if i < filled else "◇" for i in range(_GRID_CELLS)]
        rows = [
            " ".join(cells[r * _GRID_COLS : (r + 1) * _GRID_COLS])
            for r in range(_GRID_ROWS)
        ]
        return "\n".join(rows)

    def _row(
        self,
        mark: str,
        label: str,
        tokens: int,
        max_tokens: int,
        *,
        extra: str = "",
        style: str | None = None,
    ) -> Text:
        # 固定列宽，便于扫读
        left = f"{mark} {label:<16}"
        mid = f"{abbrev_tokens(tokens):>6} tokens"
        if max_tokens > 0:
            pct = f"({tokens / max_tokens * 100:5.1f}%)"
        else:
            pct = ""
        line = f"{left}  {mid}  {pct}{extra}".rstrip()
        return Text(line, style=style) if style else Text(line)

    def _render_turn(self, title: str, turn: TurnUsage) -> RenderableType:
        lines: list[RenderableType] = [Text(title, style="bold")]
        suffix = f"  steps={turn.steps}" if turn.steps else ""
        lines.append(
            Text(f"  actual_input={abbrev_tokens(turn.actual_input_tokens)}{suffix}")
        )
        lines.append(
            Text(
                f"    in={abbrev_tokens(turn.input_tokens)} "
                f"cache_r={abbrev_tokens(turn.cache_read_tokens)} "
                f"cache_w={abbrev_tokens(turn.cache_write_tokens)}"
            )
        )
        lines.append(Text(f"  out={abbrev_tokens(turn.output_tokens)}"))
        if turn.elapsed_ms:
            lines.append(Text(f"  duration={turn.elapsed_ms / 1000:.1f}s"))
        if turn.model_label:
            lines.append(Text(f"  model={turn.model_label}"))
        return Group(*lines)


def _sum_keys(by_key: dict[str, ContextCategory], *keys: str) -> int:
    return sum(by_key[key].tokens for key in keys if key in by_key)


def _detail_count(category: ContextCategory | None) -> int:
    if category is None:
        return 0
    return len(category.details)


def _occupied_cells(total_tokens: int, max_input_tokens: int | None) -> int:
    """占用格数：0..100。有占用时至少 1 格，避免 0.2% 时图上全空。"""
    if max_input_tokens in (None, 0):
        return 0
    if total_tokens <= 0:
        return 0
    ratio = max(0.0, min(1.0, total_tokens / max_input_tokens))
    filled = round(_GRID_CELLS * ratio)
    return max(1, min(_GRID_CELLS, filled))
