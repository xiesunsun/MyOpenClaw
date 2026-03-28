from pathlib import Path
from typing import Callable

from myopenclaw.agent.agent import Agent
from myopenclaw.app.bootstrap import AppBootstrap
from myopenclaw.llm.provider import ChatResult, MessageMetadata
from rich.console import Console, Group, RenderableType
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text


class ChatLoop:
    def __init__(
        self,
        agent: Agent,
        agent_id: str | None = None,
        session=None,
        config_path: Path | None = None,
        console: Console | None = None,
        input_reader: Callable[[str], str] | None = None,
    ) -> None:
        self.agent = agent
        self.agent_id = agent_id or agent.definition.agent_id
        self.session = session or agent.new_session()
        self.config_path = config_path
        self.console = console or Console()
        self.input_reader = input_reader or self._default_input_reader
        self._fallback_message_count = self._read_session_message_count()

    @classmethod
    def from_config_path(
        cls,
        config_path: Path,
        agent_id: str | None = None,
    ) -> "ChatLoop":
        loaded_runtime = AppBootstrap.from_config_path(
            config_path=config_path,
            agent_id=agent_id,
        )
        return cls(
            agent=loaded_runtime.agent,
            agent_id=loaded_runtime.agent_id,
            config_path=config_path,
        )

    async def handle_user_input(self, text: str) -> ChatResult:
        return await self.session.send_user_message(text)

    def _default_input_reader(self, prompt: str) -> str:
        return self.console.input(f"[bold cyan]{prompt}[/bold cyan]")

    def _read_session_message_count(self) -> int:
        state = getattr(self.session, "state", None)
        messages = getattr(state, "messages", None)
        return len(messages) if messages is not None else 0

    def _message_count(self) -> int:
        state_count = self._read_session_message_count()
        return state_count if state_count else self._fallback_message_count

    def _render_header(self) -> None:
        body = Group(
            Text(f"Agent: {self.agent_id}", style="bold cyan"),
            Text(
                f"Config: {self.config_path}"
                if self.config_path
                else "Config: default",
                style="dim",
            ),
            Text("/help  /clear  /session  /exit", style="yellow"),
        )
        self.console.print(
            Panel(
                body,
                title="MyOpenClaw Chat",
                border_style="bright_blue",
                expand=True,
            )
        )

    def _render_system_message(self, text: str, *, style: str = "cyan") -> None:
        self.console.print(
            Panel(
                Text(text),
                title="System",
                border_style=style,
                expand=True,
            )
        )

    def _render_error_message(self, text: str) -> None:
        self._render_system_message(text, style="red")

    def _render_message(self, title: str, content: RenderableType, *, style: str) -> None:
        self.console.print(
            Panel(
                content,
                title=title,
                border_style=style,
                expand=True,
            )
        )

    def _render_assistant_message(self, reply: ChatResult) -> None:
        content: RenderableType = Markdown(reply.text)
        metadata = reply.metadata
        if metadata is not None:
            content = Group(Markdown(reply.text), self._render_assistant_footer(metadata))
        self._render_message("Assistant", content, style="yellow")

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

    def _render_help(self) -> None:
        help_text = Text.from_markup(
            "[bold]Available commands[/bold]\n"
            "/help    Show this help message\n"
            "/clear   Clear the screen and redraw the header\n"
            "/session Show current session details\n"
            "/exit    Exit the chat loop"
        )
        self._render_message("System", help_text, style="cyan")

    def _render_session_summary(self) -> None:
        summary = Text(
            f"Agent: {self.agent_id}\nMessages: {self._message_count()}",
        )
        self._render_message("System", summary, style="cyan")

    def _handle_command(self, user_input: str) -> bool:
        command = user_input.lower()
        if command == "/help":
            self._render_help()
            return True
        if command == "/session":
            self._render_session_summary()
            return True
        if command == "/clear":
            self.console.clear(home=True)
            self._render_header()
            return True
        if command == "/exit":
            self._render_system_message("Session closed.")
            return False

        self._render_error_message(f"Unknown command: {user_input}. Try /help.")
        return True

    async def run(self) -> None:
        self._render_header()
        while True:
            try:
                user_input = self.input_reader("You > ").strip()
            except (EOFError, KeyboardInterrupt):
                self._render_system_message("Session closed.")
                break

            if user_input.lower() in {"quit", "exit"}:
                self._render_system_message("Session closed.")
                break
            if not user_input:
                continue
            if user_input.startswith("/"):
                if not self._handle_command(user_input):
                    break
                continue

            self._render_message("You", Text(user_input), style="cyan")
            self._fallback_message_count += 1
            try:
                with self.console.status("[bold yellow]Thinking...[/bold yellow]", spinner="dots"):
                    reply = await self.handle_user_input(user_input)
            except Exception as exc:
                self._render_error_message(f"Request failed: {exc}")
                continue

            self._fallback_message_count += 1
            self._render_assistant_message(reply)
