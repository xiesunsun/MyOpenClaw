from rich.console import Console, Group, RenderableType
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text

from pickel.runs.runtime_events import (
    AssistantMessageEvent,
    RuntimeEventBase,
    StepStarted,
    ToolCallCompleted,
    ToolCallStarted,
)
from pickel.runs.turn_usage import TurnUsage
from pickel.tools.base import ToolExecutionResult


class ChatEventRenderer:
    def __init__(self, console: Console) -> None:
        self.console = console
        self.rendered_assistant_message = False

    async def handle_event(self, event: RuntimeEventBase) -> None:
        if isinstance(event, StepStarted):
            self._render_message(
                "Thinking",
                Text(f"Step {event.envelope.step_index}"),
                style="magenta",
            )
            return

        if isinstance(event, ToolCallStarted) and event.tool_call is not None:
            self._render_message(
                "Tool",
                self._render_tool_started(event.tool_call.name, event.tool_call.arguments),
                style="blue",
            )
            return

        if isinstance(event, ToolCallCompleted) and event.tool_call is not None:
            tool_result = event.tool_result or ToolExecutionResult(content="")
            self._render_message(
                "Tool",
                self._render_tool_finished(
                    event.tool_call.name, event.tool_call.arguments, tool_result
                ),
                style="red" if tool_result.is_error else "green",
            )
            return

        if isinstance(event, AssistantMessageEvent):
            self.rendered_assistant_message = True
            content: RenderableType = Markdown(event.text)
            if event.usage is not None:
                content = Group(
                    Markdown(event.text), self._render_assistant_footer(event.usage)
                )
            self._render_message("Assistant", content, style="yellow")

    @classmethod
    def _render_tool_started(cls, name: str, arguments: dict[str, object]) -> Text:
        return Text(
            f"{cls._format_tool_label(name, arguments)}\n"
            "status: running"
        )

    @classmethod
    def _render_tool_finished(
        cls,
        name: str,
        arguments: dict[str, object],
        tool_result: ToolExecutionResult,
    ) -> Text:
        status = "failed" if tool_result.is_error else "ok"
        lines = [
            cls._format_tool_label(name, arguments),
            f"status: {status}",
        ]
        if tool_result.content:
            lines.append(f"result: {cls._truncate_content(tool_result.content)}")
        return Text("\n".join(lines))

    def _render_message(self, title: str, content: RenderableType, *, style: str) -> None:
        self.console.print(
            Panel(
                content,
                title=title,
                border_style=style,
                expand=True,
            )
        )

    def _render_assistant_footer(self, usage: TurnUsage) -> Text:
        footer = Text(style="dim", justify="right")
        if usage.model_label:
            footer.append(usage.model_label)
        stats = [
            f"in {usage.actual_input_tokens}",
            f"out {usage.output_tokens}",
        ]
        if usage.elapsed_ms:
            stats.append(f"{usage.elapsed_ms / 1000:.1f}s")
        footer.append("\n")
        footer.append(" · ".join(stats))
        return footer

    @staticmethod
    def _format_tool_label(name: str, arguments: dict[str, object]) -> str:
        parts: list[str] = []
        for key, value in arguments.items():
            rendered = repr(value)
            if key == "content":
                rendered = f"<{len(str(value))} chars>"
            elif len(rendered) > 100:
                rendered = f"{rendered[:97]}..."
            parts.append(f"{key}={rendered}")
        return f"{name}({', '.join(parts)})"

    @staticmethod
    def _truncate_content(content: str, limit: int = 180) -> str:
        normalized = " ".join(content.split())
        if len(normalized) <= limit:
            return normalized
        return f"{normalized[:limit - 3]}..."
