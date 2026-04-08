from rich.console import Console, Group, RenderableType
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text

from myopenclaw.conversations.message import ToolCallBatch
from myopenclaw.conversations.metadata import MessageMetadata
from myopenclaw.runs import RuntimeEvent, RuntimeEventType
from myopenclaw.tools.base import ToolExecutionResult


class ChatEventRenderer:
    def __init__(self, console: Console) -> None:
        self.console = console
        self.rendered_assistant_message = False

    async def handle_event(self, event: RuntimeEvent) -> None:
        if event.event_type == RuntimeEventType.MODEL_STEP_STARTED:
            self._render_message(
                "Thinking",
                Text(f"Step {event.step_index}"),
                style="magenta",
            )
            return

        if event.event_type == RuntimeEventType.TOOL_CALL_STARTED and event.tool_call is not None:
            self._render_message(
                "Tool",
                self._render_tool_started(event.tool_call.name, event.tool_call.arguments),
                style="blue",
            )
            return

        if event.event_type in {
            RuntimeEventType.TOOL_CALL_COMPLETED,
            RuntimeEventType.TOOL_CALL_FAILED,
        } and event.tool_call is not None:
            tool_result = event.tool_result or ToolExecutionResult(content="")
            self._render_message(
                "Tool",
                self._render_tool_finished(
                    event.tool_call.name,
                    event.tool_call.arguments,
                    tool_result,
                ),
                style="red" if tool_result.is_error else "green",
            )
            return

        if event.event_type == RuntimeEventType.ASSISTANT_MESSAGE:
            self.rendered_assistant_message = True
            content: RenderableType = Markdown(event.text)
            if event.metadata is not None:
                content = Group(Markdown(event.text), self._render_assistant_footer(event.metadata))
            self._render_message("Assistant", content, style="yellow")

    @classmethod
    def render_tool_batch_transcript(cls, batch: ToolCallBatch) -> list[tuple[str, Text]]:
        results_by_call_id = {result.call_id: result for result in batch.results}
        entries: list[tuple[str, Text]] = []
        for tool_call in batch.calls:
            tool_result = results_by_call_id.get(tool_call.id)
            if tool_result is None:
                continue
            renderable = cls._render_tool_finished(
                tool_call.name,
                tool_call.arguments,
                ToolExecutionResult(
                    content=tool_result.content,
                    is_error=tool_result.is_error,
                    metadata=dict(tool_result.metadata),
                ),
            )
            entries.append(("red" if tool_result.is_error else "green", renderable))
        return entries

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

    def _render_assistant_footer(self, metadata: MessageMetadata) -> Text:
        footer = Text(style="dim", justify="right")
        footer.append(f"{metadata.provider} / {metadata.model}")
        stats = []
        if metadata.input_tokens is not None:
            stats.append(f"in {metadata.input_tokens}")
        if metadata.output_tokens is not None:
            stats.append(f"out {metadata.output_tokens}")
        if metadata.elapsed_ms is not None:
            stats.append(f"{metadata.elapsed_ms / 1000:.1f}s")
        if stats:
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
