from __future__ import annotations

from pathlib import Path
from typing import Callable

from rich.console import Console, Group, RenderableType
from rich.panel import Panel
from rich.text import Text

from myopenclaw.application import RuntimeEventHandler, TurnResult
from myopenclaw.application.services import ChatService
from myopenclaw.interfaces.cli.event_renderer import ChatEventRenderer


class ChatLoop:
    def __init__(
        self,
        chat_service: ChatService,
        config_path: Path | None = None,
        console: Console | None = None,
        input_reader: Callable[[str], str] | None = None,
    ) -> None:
        self.chat_service = chat_service
        self.agent_id = chat_service.agent.agent_id
        self.config_path = config_path
        self.console = console or Console()
        self.input_reader = input_reader or self._default_input_reader
        self.renderer = ChatEventRenderer(self.console)

    async def handle_user_input(
        self,
        text: str,
        event_handler: RuntimeEventHandler | None = None,
    ) -> TurnResult:
        return await self.chat_service.run_turn(text, event_handler=event_handler)

    def create_event_handler(self) -> RuntimeEventHandler:
        return self.renderer.handle_event

    def render_turn_output(self, turn_result: TurnResult, *, start_index: int = 0) -> None:
        del start_index
        self.renderer.render_turn_output(turn_result)

    def _default_input_reader(self, prompt: str) -> str:
        return self.console.input(f"[bold cyan]{prompt}[/bold cyan]")

    def _message_count(self) -> int:
        return len(self.chat_service.session.messages)

    def _render_header(self) -> None:
        body = Group(
            Text(f"Agent: {self.agent_id}", style="bold cyan"),
            Text(
                f"Config: {self.config_path}" if self.config_path else "Config: default",
                style="dim",
            ),
            Text("/help  /clear  /session  /exit", style="yellow"),
        )
        self.console.print(
            Panel(body, title="MyOpenClaw Chat", border_style="bright_blue", expand=True)
        )

    def _render_system_message(self, text: str, *, style: str = "cyan") -> None:
        self.console.print(
            Panel(Text(text), title="System", border_style=style, expand=True)
        )

    def _render_error_message(self, text: str) -> None:
        self._render_system_message(text, style="red")

    def _render_message(self, title: str, content: RenderableType, *, style: str) -> None:
        self.console.print(
            Panel(content, title=title, border_style=style, expand=True)
        )

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
        summary = Text(f"Agent: {self.agent_id}\nMessages: {self._message_count()}")
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
            try:
                turn_result = await self.handle_user_input(
                    user_input,
                    event_handler=self.create_event_handler(),
                )
            except Exception as exc:
                self._render_error_message(f"Request failed: {exc}")
                continue

            if not turn_result.metadata and not turn_result.text:
                self.renderer.render_turn_output(turn_result)
