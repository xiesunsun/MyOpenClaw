from pathlib import Path
from typing import Callable

from myopenclaw.agent.agent import Agent
from myopenclaw.app.builder import AgentBuilder
from myopenclaw.config.app_config import AppConfig
from myopenclaw.conversation.message import SessionMessage, ToolCallBatch
from myopenclaw.conversation.metadata import MessageMetadata
from myopenclaw.conversation.session import Session
from myopenclaw.interfaces.cli.event_renderer import ChatEventRenderer
from myopenclaw.llm import GenerateResult
from myopenclaw.runtime import RuntimeEventHandler, AgentCoordinator, ReActStrategy
from rich.console import Console, Group, RenderableType
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text


class ChatLoop:
    def __init__(
        self,
        agent: Agent,
        agent_id: str | None = None,
        coordinator: AgentCoordinator | None = None,
        session: Session | None = None,
        config_path: Path | None = None,
        console: Console | None = None,
        input_reader: Callable[[str], str] | None = None,
    ) -> None:
        self.agent = agent
        self.agent_id = agent_id or agent.agent_id
        self.coordinator = coordinator or AgentCoordinator(strategy=ReActStrategy())
        self.session = session or Session.create(agent_id=self.agent_id)
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
        app_config = AppConfig.load(config_path)
        agent = AgentBuilder.build_from_app_config(
            app_config=app_config,
            agent_id=agent_id,
        )
        return cls(
            agent=agent,
            agent_id=agent.agent_id,
            coordinator=AgentCoordinator(strategy=ReActStrategy(max_steps=app_config.react_max_steps)),
            config_path=config_path,
        )

    async def handle_user_input(
        self,
        text: str,
        event_handler: RuntimeEventHandler | None = None,
    ) -> GenerateResult:
        return await self.coordinator.run_turn(
            agent=self.agent,
            session=self.session,
            user_text=text,
            event_handler=event_handler,
        )

    def create_event_handler(self) -> RuntimeEventHandler:
        return ChatEventRenderer(self.console).handle_event

    def render_turn_output(self, reply: GenerateResult, *, start_index: int) -> None:
        for message in self.session.messages[start_index:]:
            if message.tool_call_batch is not None:
                self._render_tool_batch(message.tool_call_batch)
        self._render_assistant_message(reply)

    def _default_input_reader(self, prompt: str) -> str:
        return self.console.input(f"[bold cyan]{prompt}[/bold cyan]")

    def _read_session_message_count(self) -> int:
        return len(self.session.messages)

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

    def _render_assistant_message(self, reply: GenerateResult) -> None:
        content: RenderableType = Markdown(reply.text)
        metadata = reply.metadata
        if metadata is not None:
            content = Group(Markdown(reply.text), self._render_assistant_footer(metadata))
        self._render_message("Assistant", content, style="yellow")

    def _render_tool_batch(self, batch: ToolCallBatch) -> None:
        for style, renderable in ChatEventRenderer.render_tool_batch_transcript(batch):
            self._render_message("Tool", renderable, style=style)

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
                reply = await self.handle_user_input(
                    user_input,
                    event_handler=self.create_event_handler(),
                )
            except Exception as exc:
                self._render_error_message(f"Request failed: {exc}")
                continue

            self._fallback_message_count += 1
            if not reply.metadata and not reply.text:
                self._render_assistant_message(reply)
