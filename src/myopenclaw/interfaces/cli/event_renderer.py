from rich.console import Console, Group, RenderableType
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text

from myopenclaw.conversation.metadata import MessageMetadata
from myopenclaw.runtime import RuntimeEvent, RuntimeEventType


class ChatEventRenderer:
    def __init__(self, console: Console) -> None:
        self.console = console

    async def handle_event(self, event: RuntimeEvent) -> None:
        if event.event_type == RuntimeEventType.MODEL_STEP_STARTED:
            self._render_message(
                "Thinking",
                Text(f"Step {event.step_index}"),
                style="magenta",
            )
            return

        if event.event_type == RuntimeEventType.TOOL_CALL_STARTED and event.tool_call is not None:
            body = Text(
                f"{event.tool_call.name}({self._format_tool_arguments(event.tool_call.arguments)})"
            )
            self._render_message("Tool Call", body, style="blue")
            return

        if event.event_type == RuntimeEventType.TOOL_CALL_COMPLETED and event.tool_call is not None:
            tool_result = event.tool_result
            status = "error" if tool_result and tool_result.is_error else "ok"
            content = tool_result.content if tool_result is not None else ""
            body = Text(f"{event.tool_call.name} -> {status}\n{self._truncate_content(content)}")
            if tool_result is not None and tool_result.metadata:
                body.append(f"\n{self._format_tool_metadata(tool_result.metadata)}", style="dim")
            self._render_message(
                "Tool Result",
                body,
                style="red" if tool_result and tool_result.is_error else "green",
            )
            return

        if event.event_type == RuntimeEventType.ASSISTANT_MESSAGE:
            content: RenderableType = Markdown(event.text)
            if event.metadata is not None:
                content = Group(Markdown(event.text), self._render_assistant_footer(event.metadata))
            self._render_message("Assistant", content, style="yellow")

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

    def _format_tool_arguments(self, arguments: dict[str, object]) -> str:
        formatted = []
        for key, value in arguments.items():
            rendered = repr(value)
            if len(rendered) > 72:
                rendered = f"{rendered[:69]}..."
            formatted.append(f"{key}={rendered}")
        return ", ".join(formatted)

    def _truncate_content(self, content: str, limit: int = 120) -> str:
        if len(content) <= limit:
            return content
        return f"{content[:limit]}..."

    def _format_tool_metadata(self, metadata: dict[str, object]) -> str:
        parts: list[str] = []
        exit_code = metadata.get("exit_code")
        if exit_code is not None:
            parts.append(f"exit {exit_code}")
        cwd = metadata.get("cwd")
        if cwd:
            parts.append(f"cwd {cwd}")
        shell_status = metadata.get("shell_status")
        if shell_status:
            parts.append(f"status {shell_status}")
        timed_out = metadata.get("timed_out")
        if timed_out:
            parts.append("timed out")
        truncated = metadata.get("truncated")
        if truncated:
            parts.append("truncated")
        return " · ".join(parts)
