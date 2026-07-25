from __future__ import annotations

import inspect
import traceback
from pathlib import Path
from typing import Awaitable, Callable

from myopenclaw.app.boot import Boot
from myopenclaw.cli.context_renderer import ContextRenderer, ModelContextRenderer
from myopenclaw.context.model_context import SystemContent, ToolDefinition
from myopenclaw.context.observation import ContextObservation
from myopenclaw.conversations.service import SessionService
from myopenclaw.conversations.session_storage_mapper import build_session_preview
from myopenclaw.conversations.agent_message import AssistantMessage
from myopenclaw.conversations.content_blocks import TextContent
from myopenclaw.conversations.metadata import MessageMetadata
from myopenclaw.conversations.session import Session
from myopenclaw.cli.event_renderer import ChatEventRenderer
from myopenclaw.cli.prompt_input import PromptToolkitInputReader
from myopenclaw.runs import (
    RuntimeEventHandler,
)
from myopenclaw.runs.context_usage import ContextUsageService
from myopenclaw.runs.run import Run
from rich.console import Console, Group, RenderableType
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text


class ChatLoop:
    def __init__(
        self,
        agent: "Agent",
        agent_id: str | None = None,
        run: Run | None = None,
        session: Session | None = None,
        config_path: Path | None = None,
        console: Console | None = None,
        input_reader: Callable[[str], str | Awaitable[str]] | None = None,
        context_usage_service: ContextUsageService | None = None,
        context_renderer: ContextRenderer | None = None,
        session_service: SessionService | None = None,
    ) -> None:
        self.agent = agent
        self.agent_id = agent_id or agent.agent_id
        self._run = run
        self.session = session or Session.create(agent_id=self.agent_id)
        self.config_path = config_path
        self.console = console or Console()
        self._prompt_input_reader: PromptToolkitInputReader | None = None
        self.input_reader = input_reader or self._default_input_reader
        self._fallback_message_count = self._read_session_message_count()
        self._context_usage_service = context_usage_service or ContextUsageService()
        self._context_renderer = context_renderer or ContextRenderer()
        self._session_service = session_service
        self._session_closed = False

    @classmethod
    def from_config_path(
        cls,
        config_path: Path,
        agent_id: str | None = None,
        session_id: str | None = None,
    ) -> "ChatLoop":
        boot = Boot.from_config_path(config_path)
        if session_id is not None:
            session_service = boot.build_session_service()
            session = session_service.resume(session_id=session_id)
            agent, run = boot.build_run(agent_id=session.agent_id)
            session_service = boot.build_session_service(agent_id=session.agent_id)
        else:
            agent, run = boot.build_run(agent_id=agent_id)
            session_service = boot.build_session_service(agent_id=agent.agent_id)
            # 新会话绑定当前工作目录，供 sessions 列表默认过滤
            session = session_service.start(
                agent_id=agent.agent_id,
                cwd=str(Path.cwd().resolve()),
            )
        if run.session_service is None:
            run.session_service = session_service
        return cls(
            agent=agent,
            agent_id=agent.agent_id,
            run=run,
            session=session,
            config_path=config_path,
            session_service=session_service,
        )

    async def handle_user_input(
        self,
        text: str,
        event_handler: RuntimeEventHandler | None = None,
    ) -> AssistantMessage:
        if self._run is None:
            raise ValueError("Run 未提供")
        return await self._run.turn(
            session=self.session,
            user_text=text,
            event_handler=event_handler,
        )

    def create_event_handler(self) -> RuntimeEventHandler:
        return ChatEventRenderer(self.console).handle_event

    def render_turn_output(self, reply: AssistantMessage, *, start_index: int) -> None:
        for entry in self.session.entries[start_index:]:
            payload = entry.payload if isinstance(entry.payload, dict) else {}
            if payload.get("role") == "tool":
                tool_text = ""
                for block in payload.get("content") or []:
                    if isinstance(block, dict) and block.get("type") == "text":
                        tool_text = block.get("text") or tool_text
                name = payload.get("tool_name") or "tool"
                self.console.print(Text(f"[{name}] {tool_text[:200]}", style="dim"))
        self._render_assistant_message(reply)

    async def _default_input_reader(self, prompt: str) -> str:
        if self._prompt_input_reader is None:
            self._prompt_input_reader = PromptToolkitInputReader()
        return await self._prompt_input_reader(prompt)

    def _read_session_message_count(self) -> int:
        return len(self.session.entries)

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
            Text("/help  /context  /clear  /session  /exit", style="yellow"),
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

    def _render_assistant_message(self, reply: AssistantMessage) -> None:
        text_parts = [
            block.text
            for block in reply.content
            if isinstance(block, TextContent) and block.text
        ]
        body_text = chr(10).join(text_parts)
        metadata = None
        if reply.metadata is not None:
            usage = reply.metadata.usage
            metadata = MessageMetadata(
                provider=reply.metadata.provider,
                model=reply.metadata.model,
                input_tokens=usage.input_tokens if usage else None,
                output_tokens=usage.output_tokens if usage else None,
                total_tokens=usage.total_tokens if usage else None,
                elapsed_ms=reply.metadata.elapsed_ms,
                provider_finish_reason=reply.metadata.finish_reason,
                provider_finish_message=reply.metadata.finish_message,
                provider_response_id=reply.metadata.provider_response_id,
                provider_model_version=reply.metadata.provider_model_version,
            )
        content: RenderableType = Markdown(body_text)
        if metadata is not None:
            content = Group(Markdown(body_text), self._render_assistant_footer(metadata))
        self._render_message("Assistant", content, style="yellow")

    def _render_tool_batch(self, batch) -> None:
        return


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
            "/context Show current context usage summary\n"
            "/clear   Clear the screen and redraw the header\n"
            "/session Show current session details\n"
            "/exit    Exit the chat loop"
        )
        self._render_message("System", help_text, style="cyan")

    def _render_session_summary(self) -> None:
        preview = (
            self._session_service.build_preview(session=self.session)
            if self._session_service is not None
            else build_session_preview(session=self.session)
        )
        summary = Text(
            "\n".join(
                [
                    f"Session ID: {preview.session_id}",
                    f"Agent: {preview.agent_id}",
                    f"Status: {preview.status}",
                    f"Messages: {preview.message_count}",
                    f"Updated: {preview.updated_at.isoformat()}",
                    f"Last message: {preview.last_message or '-'}",
                ]
            ),
        )
        self._render_message("System", summary, style="cyan")

    def _close_session(self) -> None:
        if self._session_closed:
            return
        if self._session_service is not None:
            self._session_service.close(session=self.session)
        self._session_closed = True

    async def _handle_command(self, user_input: str) -> bool:
        command = user_input.lower()
        if command == "/help":
            self._render_help()
            return True
        if command == "/context":
            await self._render_context_command()
            return True
        if command == "/session":
            self._render_session_summary()
            return True
        if command == "/clear":
            self.console.clear(home=True)
            self._render_header()
            return True
        if command == "/exit":
            self._close_session()
            self._render_system_message("Session closed.")
            return False

        self._render_error_message(f"Unknown command: {user_input}. Try /help.")
        return True

    async def _render_context_command(self) -> None:
        run = self._run
        last_meta = getattr(self, "_last_assistant_metadata", None)
        if run is None:
            observation = ContextObservation(
                model_context=None,
                predicted=True,
                note="尚无 Run",
            )
        else:
            system = SystemContent.from_text(self.agent.system_instruction or "")
            tools = []
            for tool in getattr(run, "tools", []) or []:
                spec = getattr(tool, "spec", None)
                if spec is None:
                    continue
                tools.append(
                    ToolDefinition(
                        name=spec.name,
                        description=getattr(spec, "description", "") or "",
                        input_schema=getattr(spec, "input_schema", {}) or {},
                    )
                )
            # 不触发 Hook：hook_feedback 传空
            try:
                ctx = run.context_assembler.assemble(
                    entries=self.session.active_path(),
                    system=system,
                    tools=tools,
                    hook_feedback=[],
                    unit_window=run.unit_window,
                )
            except Exception as exc:  # 测试 mock / 不完整 run
                observation = ContextObservation(
                    model_context=None,
                    predicted=True,
                    assistant_metadata=last_meta,
                    note=f"组装失败: {exc}",
                )
            else:
                predicted = last_meta is None
                observation = ContextObservation(
                    model_context=ctx,
                    predicted=predicted,
                    assistant_metadata=last_meta,
                    note=None if not predicted else "尚无实际模型调用；展示预测组装",
                )
        renderable = ModelContextRenderer().render_observation(observation)
        self._render_message("System", renderable, style="cyan")

    async def run(self) -> None:
        self._render_header()
        while True:
            try:
                raw_user_input = self.input_reader("You > ")
                if inspect.isawaitable(raw_user_input):
                    raw_user_input = await raw_user_input
                user_input = raw_user_input.strip()
            except (EOFError, KeyboardInterrupt):
                self._close_session()
                self._render_system_message("Session closed.")
                break

            if user_input.lower() in {"quit", "exit"}:
                self._close_session()
                self._render_system_message("Session closed.")
                break
            if not user_input:
                continue
            if user_input.startswith("/"):
                if not await self._handle_command(user_input):
                    break
                continue

            self._fallback_message_count += 1
            event_renderer = ChatEventRenderer(self.console)
            start_index = len(self.session.entries)
            try:
                reply = await self.handle_user_input(
                    user_input,
                    event_handler=event_renderer.handle_event,
                )
                if self._session_service is not None:
                    self._session_service.flush_new_entries(
                        session=self.session,
                        entries=[],
                    )
            except Exception as exc:
                self._render_error_message(traceback.format_exc().rstrip())
                continue

            self._fallback_message_count += 1
            if not event_renderer.rendered_assistant_message:
                self._render_assistant_message(reply)
