from __future__ import annotations

import inspect
import traceback
from pathlib import Path
from typing import TYPE_CHECKING, Awaitable, Callable

from pickel.app.boot import Boot
from pickel.cli.context_renderer import ContextRenderer
from pickel.config.app_config import AppConfig
from pickel.config.loader import Config
from pickel.context.prepare import prepare
from pickel.conversations.service import SessionService
from pickel.conversations.session_storage_mapper import build_session_preview
from pickel.conversations.agent_message import AssistantMessage
from pickel.conversations.content_blocks import TextContent
from pickel.conversations.metadata import MessageMetadata
from pickel.conversations.session import Session
from pickel.cli.event_renderer import ChatEventRenderer
from pickel.cli.prompt_input import PromptToolkitInputReader
from pickel.runs import (
    RuntimeEventHandler,
)
from pickel.runs.measure import measure
from pickel.runs.run import Run
from pickel.runs.turn_usage import last_turn_usage, session_usage
from pickel.runs.usage_anchor import resolve_anchor
from pickel.shared.model_config import ModelSelection
from rich.console import Console, Group, RenderableType
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text

if TYPE_CHECKING:
    from pickel.agents.agent import Agent


class ChatLoop:
    def __init__(
        self,
        agent: "Agent",
        agent_id: str | None = None,
        run: Run | None = None,
        session: Session | None = None,
        console: Console | None = None,
        input_reader: Callable[[str], str | Awaitable[str]] | None = None,
        context_renderer: ContextRenderer | None = None,
        session_service: SessionService | None = None,
        boot: Boot | None = None,
        app_config: AppConfig | None = None,
    ) -> None:
        self.agent = agent
        self.agent_id = agent_id or agent.agent_id
        self._run = run
        self.session = session or Session.create(agent_id=self.agent_id)
        self.console = console or Console()
        self._prompt_input_reader: PromptToolkitInputReader | None = None
        self.input_reader = input_reader or self._default_input_reader
        self._fallback_message_count = self._read_session_message_count()
        self._context_renderer = context_renderer or ContextRenderer()
        self._session_service = session_service
        self._session_closed = False
        self._boot = boot
        self._app_config = app_config or (boot.app_config if boot is not None else None)

    @classmethod
    def from_boot(
        cls,
        boot: Boot,
        agent_id: str | None = None,
        session_id: str | None = None,
    ) -> "ChatLoop":
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
            session_service=session_service,
            boot=boot,
            app_config=boot.app_config,
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
            Text("Config: ~/.pickel + project .pickel / agents", style="dim"),
            Text(
                "/help  /model  /thinking  /agent  /new  /reload  /context  /session  /clear  /exit",
                style="yellow",
            ),
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
            "/help              Show this help message\n"
            "/model [p/m]       List or set provider/model (Environ)\n"
            "/thinking <level>  Set thinking level in Environ\n"
            "/agent [id]        List agents or switch (new empty Session)\n"
            "/new               New empty Session, same agent\n"
            "/reload            Reload disk config/skills/agent (keep Environ)\n"
            "/context           Show context usage (preview) and API usage\n"
            "/session           Show current session details\n"
            "/clear             Clear the screen and redraw the header\n"
            "/exit              Exit the chat loop"
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

    def _list_available_models(self) -> list[str]:
        """从 app_config.providers 列出 provider/model。"""
        if self._app_config is None:
            return []
        lines: list[str] = []
        for provider_id in sorted(self._app_config.providers):
            catalog = self._app_config.providers[provider_id]
            for model_id in sorted(catalog.models):
                lines.append(f"{provider_id}/{model_id}")
        return lines

    def _parse_model_arg(self, arg: str) -> ModelSelection:
        """解析 provider/model；provider 可含斜杠，按已知 providers 前缀匹配。"""
        if self._app_config is None:
            raise ValueError("AppConfig 未提供，无法解析 model")
        providers = self._app_config.providers
        # 长前缀优先，避免短 id 误匹配
        for provider_id in sorted(providers, key=len, reverse=True):
            prefix = f"{provider_id}/"
            if arg.startswith(prefix):
                model = arg[len(prefix) :]
                if model in providers[provider_id].models:
                    return ModelSelection(provider=provider_id, model=model)
                raise KeyError(
                    f"Unknown model '{model}' for provider '{provider_id}'"
                )
        # 仅 model 名且全局唯一
        matches: list[ModelSelection] = []
        for provider_id, catalog in providers.items():
            if arg in catalog.models:
                matches.append(ModelSelection(provider=provider_id, model=arg))
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            opts = ", ".join(f"{m.provider}/{m.model}" for m in matches)
            raise ValueError(f"model '{arg}' 不唯一，请指定 provider/model：{opts}")
        raise KeyError(f"Unknown model selection: {arg}")

    def _handle_model_command(self, arg: str | None) -> None:
        if self._run is None:
            self._render_error_message("Run 未提供")
            return
        if self._app_config is None:
            self._render_error_message("AppConfig 未提供，无法使用 /model")
            return
        if not arg:
            lines = self._list_available_models()
            if not lines:
                self._render_system_message("无可用模型。")
                return
            current = (
                f"{self._run.agent.model_config.provider}/"
                f"{self._run.agent.model_config.model}"
            )
            body = "Available models:\n" + "\n".join(lines) + f"\n\nCurrent: {current}"
            self._render_system_message(body)
            return
        try:
            selection = self._parse_model_arg(arg)
            self._run.environ.llm = selection
            self._run.apply_environ_model(self._app_config)
            self._render_system_message(
                f"Model set to {selection.provider}/{selection.model}"
            )
        except (KeyError, ValueError) as exc:
            self._render_error_message(str(exc))

    def _handle_thinking_command(self, arg: str | None) -> None:
        if self._run is None:
            self._render_error_message("Run 未提供")
            return
        if self._app_config is None:
            self._render_error_message("AppConfig 未提供，无法使用 /thinking")
            return
        if not arg:
            current = self._run.environ.provider_options.get("thinking")
            self._render_system_message(
                f"thinking: {current if current is not None else '(unset)'}\n"
                "Usage: /thinking <level>"
            )
            return
        level = arg.strip()
        self._run.environ.provider_options["thinking"] = level
        self._run.apply_environ_model(self._app_config)
        self._render_system_message(f"thinking set to {level}")

    def _handle_agent_command(self, arg: str | None) -> None:
        if self._app_config is None or self._boot is None:
            self._render_error_message("Boot/AppConfig 未提供，无法使用 /agent")
            return
        if not arg:
            agent_ids = sorted(self._app_config.agents)
            if not agent_ids:
                self._render_system_message("无可用 agent。")
                return
            lines = [f"{i}. {aid}" for i, aid in enumerate(agent_ids, start=1)]
            body = (
                "Available agents:\n"
                + "\n".join(lines)
                + f"\n\nCurrent: {self.agent_id}\n"
                "use /agent <id>"
            )
            self._render_system_message(body)
            return
        agent_id = arg.strip()
        if agent_id not in self._app_config.agents:
            self._render_error_message(f"Unknown agent: {agent_id}")
            return
        try:
            session_service = self._boot.build_session_service(agent_id=agent_id)
            session = session_service.start(
                agent_id=agent_id,
                cwd=str(Path.cwd().resolve()),
            )
            agent, run = self._boot.build_run(
                agent_id=agent_id,
                session_service=session_service,
            )
            # 新 agent 不继承旧 Environ 的 model 覆盖（定义优先）；可后续 /model 改
            self.agent = agent
            self.agent_id = agent.agent_id
            self._run = run
            self.session = session
            self._session_service = session_service
            self._session_closed = False
            self._fallback_message_count = 0
            self._render_system_message(
                f"Switched to {agent_id}, new session {session.session_id}"
            )
        except Exception as exc:  # noqa: BLE001
            self._render_error_message(f"切换 agent 失败: {exc}")

    def _handle_new_command(self) -> None:
        agent_id = self.agent_id
        if self._session_service is not None:
            session = self._session_service.start(
                agent_id=agent_id,
                cwd=str(Path.cwd().resolve()),
            )
        else:
            session = Session.create(
                agent_id=agent_id,
                cwd=str(Path.cwd().resolve()),
            )
        self.session = session
        self._session_closed = False
        self._fallback_message_count = 0
        self._render_system_message(
            f"New session {session.session_id} (agent={agent_id})"
        )

    def _handle_reload_command(self) -> None:
        if self._run is None:
            self._render_error_message("Run 未提供")
            return
        try:
            app_config = Config.load(cwd=Path.cwd())
            boot = Boot.from_config(app_config)
            # 保留旧 session_service（同库/同 session）；无则新建
            session_service = self._session_service or boot.build_session_service(
                agent_id=self.agent_id
            )
            agent, new_run = Run.reload(
                boot=boot,
                old_run=self._run,
                agent_id=self.agent_id,
                session_service=session_service,
            )
            self._boot = boot
            self._app_config = app_config
            self.agent = agent
            self.agent_id = agent.agent_id
            self._run = new_run
            if self._session_service is None:
                self._session_service = session_service
            self._render_system_message(
                "Reloaded skills, templates, settings, models, agent, auth. "
                f"Session={self.session.session_id} agent={self.agent_id}. "
                "Next turn uses new snapshot."
            )
        except Exception as exc:  # noqa: BLE001 — 失败保持旧快照
            self._render_error_message(f"reload 失败，保持旧配置: {exc}")

    async def _handle_command(self, user_input: str) -> bool:
        # 仅命令名小写；参数保留大小写（model id / agent id）
        stripped = user_input.strip()
        parts = stripped.split(maxsplit=1)
        command = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else None
        if arg == "":
            arg = None

        if command == "/help":
            self._render_help()
            return True
        if command == "/model":
            self._handle_model_command(arg)
            return True
        if command == "/thinking":
            self._handle_thinking_command(arg)
            return True
        if command == "/agent":
            self._handle_agent_command(arg)
            return True
        if command == "/new":
            self._handle_new_command()
            return True
        if command == "/reload":
            self._handle_reload_command()
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
        """`/context` = ContextUsage 视图（设计 §7）。

        展示「若现在进入下一 step，prepare 将发出的 Request」的估计占用，
        外加从 Session 派生的真实 API usage。只读：不跑 hook、不执行 recall、
        不写 Session。
        """
        last_turn = last_turn_usage(self.session)
        total = session_usage(self.session)
        run = self._run

        usage = None
        note = None
        if run is None:
            note = "尚无 Run，无法组装上下文"
        else:
            try:
                request = await prepare(
                    run=run,
                    session=self.session,
                    hook_feedback=[],
                    unit_window=run.unit_window,
                    # §7.3：预览不得执行 recall（含远程 OV）
                    recall_sources=[],
                    # 预览按当前激活集现取一份快照（不在 turn 内，无缓存一致性顾虑）
                    snapshot=run.tool_bus.snapshot(run.activation),
                )
                model_config = run.agent.model_config
                usage = await measure(
                    request=request,
                    anchor=resolve_anchor(
                        session=self.session,
                        request=request,
                        provider=model_config.provider,
                        model=model_config.model,
                    ),
                    provider=run.provider,
                    model_config=model_config,
                )
            except Exception as exc:  # 测试 mock / 不完整 run
                note = f"组装失败: {exc}"

        if last_turn is None and note is None:
            note = "本会话尚未成功完成过模型调用（无 API usage）"

        renderable = self._context_renderer.render(
            usage,
            last_turn=last_turn,
            session_total=total if total is not None and total.steps > 1 else None,
            note=note,
            source_line=(
                "Source: prepare preview · hooks skipped · recall skipped · no draft input"
            ),
        )
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
            # last usage 由 /context 从 Session 派生（§11.8），不在此缓存
            if not event_renderer.rendered_assistant_message:
                self._render_assistant_message(reply)
