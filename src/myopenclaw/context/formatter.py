from __future__ import annotations

import json

from myopenclaw.context.models import ToolStep
from myopenclaw.conversations.message import ToolCallResult


class ToolStepFormatter:
    def __init__(
        self,
        *,
        tool_arguments_preview_chars: int = 100,
        tool_result_preview_chars: int = 100,
    ) -> None:
        self.tool_arguments_preview_chars = max(1, tool_arguments_preview_chars)
        self.tool_result_preview_chars = max(1, tool_result_preview_chars)

    def format_summary(self, tool_step: ToolStep) -> str:
        batch = tool_step.message.tool_call_batch
        if batch is None:
            return ""

        results_by_call_id = {
            result.call_id: result
            for result in batch.results
        }
        lines: list[str] = ["Tool step summary:"]
        for call in batch.calls:
            result = results_by_call_id.get(call.id)
            arguments_preview = self._truncate(
                json.dumps(call.arguments, sort_keys=True, ensure_ascii=False),
                self.tool_arguments_preview_chars,
            )
            result_preview = self._render_result_preview(result)
            lines.append(
                f"- {call.name} args={arguments_preview} -> {result_preview}"
            )
        return "\n".join(lines)

    def _render_result_preview(self, result: ToolCallResult | None) -> str:
        if result is None:
            return "result unavailable"
        prefix = "error" if result.is_error else "result"
        return f"{prefix}={self._truncate(result.content, self.tool_result_preview_chars)}"

    @staticmethod
    def _truncate(value: str, limit: int) -> str:
        normalized = " ".join(value.split())
        if len(normalized) <= limit:
            return normalized
        return normalized[: limit - 3].rstrip() + "..."
