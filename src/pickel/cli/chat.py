from __future__ import annotations

import asyncio
import inspect
import traceback
from pathlib import Path
from typing import TYPE_CHECKING, Awaitable, Callable

from pickel.app.boot import Boot
from pickel.app.runtime import RuntimeConversation, RuntimeHost
from pickel.cli.context_renderer import ContextRenderer
from pickel.cli.host_call_handlers import CliHostCallHandlers
from pickel.cli.slash import (
    BUILTIN_SLASH_COMMANDS,
    SlashCompleter,
    parse_slash,
)
from pickel.config.app_config import AppConfig
from pickel.config.loader import Config
from pickel.conversations.service import SessionService
from pickel.conversations.agent_message import AssistantMessage
from pickel.conversations.session import Session
from pickel.skills.store import SkillStoreError
from pickel.cli.event_renderer import ChatEventRenderer
from pickel.cli.prompt_input import PromptToolkitInputReader
from pickel.cli.render.message import render_error, render_header, render_system
from pickel.runs.event_bus import EventBus
from pickel.runs.trace_sink import JsonlTraceSink, trace_path
from pickel.runs.run import Run
from rich.console import Console
from rich.table import Table
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
        resolved_agent_id = agent_id or agent.agent_id
        resolved_session = session or Session.create(agent_id=resolved_agent_id)
        self.console = console or Console()
        self._prompt_input_reader: PromptToolkitInputReader | None = None
        self.input_reader = input_reader or self._default_input_reader
        self._context_renderer = context_renderer or ContextRenderer()
        self._host = RuntimeHost(boot) if boot is not None else None
        self._conversation = RuntimeConversation(
            agent=agent,
            run=run,
            session=resolved_session,
            session_service=session_service,
            app_config=app_config or (boot.app_config if boot is not None else None),
            # 测试与嵌入方长期从 CLI 注入 trace 路径；能力本身仍归 Conversation。
            trace_path_resolver=trace_path,
            trace_sink_factory=JsonlTraceSink,
        )
        self._host_call_handler_leases = []
        self._attach_host_call_handlers()
        self._fallback_message_count = self._read_session_message_count()
        self._slash_registry = BUILTIN_SLASH_COMMANDS
        self._slash_completer = SlashCompleter(self._slash_registry, self)

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

    # 兼容现有嵌入方的只读属性；状态真源只有 RuntimeConversation。
    @property
    def agent(self):
        return self._conversation.agent

    @property
    def agent_id(self) -> str:
        return self.agent.agent_id

    @property
    def session(self) -> Session:
        return self._conversation.session

    @property
    def _run(self):
        if hasattr(self, "_conversation"):
            return self._conversation._run
        return self.__dict__.get("_legacy_run")

    @_run.setter
    def _run(self, value) -> None:
        if hasattr(self, "_conversation"):
            self._conversation._run = value
        else:
            self.__dict__["_legacy_run"] = value

    @property
    def _session_service(self):
        return self._conversation.session_service

    @property
    def _boot(self) -> Boot | None:
        return self._host.boot if self._host is not None else None

    @property
    def _app_config(self) -> AppConfig | None:
        if self._host is not None:
            return self._host.app_config
        return self._conversation.app_config

    @property
    def _tool_bus(self):
        return self._boot.tool_bus if self._boot is not None else None

    @property
    def _bus(self) -> EventBus:
        return self._conversation.event_bus

    @property
    def _trace_sink(self):
        return self._conversation.trace_sink

    async def handle_user_input(
        self,
        text: str,
        bus: "EventBus | None" = None,
    ) -> AssistantMessage:
        if self._run is None:
            raise ValueError("Run 未提供")
        # bus 参数只为旧嵌入方兼容；正常路径始终使用 Conversation 自有 bus。
        if bus is not None and bus is not self._conversation.event_bus:
            return await self._run.turn(
                session=self.session,
                user_text=text,
                bus=bus,
            )
        return await self._conversation.turn(text)

    def create_event_bus(
        self,
    ) -> tuple[EventBus, ChatEventRenderer, Callable[[], None]]:
        """把本轮的渲染器挂到长命 bus 上。

        返回 (bus, renderer, unsubscribe)；调用方必须在 turn 结束后调 unsubscribe，
        否则渲染器会越积越多，第 N 轮的输出被打印 N 遍。
        trace sink 不在这里挂——它跟着 session 走，见 `_open_trace_sink`。
        """
        renderer = ChatEventRenderer(
            self.console,
            # usage=None 时 footer 退到这个 label；agent.model_config 在
            # /model 切换时被 run 原地更新（run.py），每轮取即最新
            fallback_model_label=(
                f"{self.agent.model_config.provider} / {self.agent.model_config.model}"
            ),
        )
        unsubscribe = self._conversation.subscribe(renderer.handle_event)
        return self._conversation.event_bus, renderer, unsubscribe

    async def _default_input_reader(self, prompt: str) -> str:
        if self._prompt_input_reader is None:
            self._prompt_input_reader = PromptToolkitInputReader()
            configured = self._prompt_input_reader.set_completer(self._slash_completer)
            # AsyncMock 兼容：真实 reader 的装配方法是同步的。
            if inspect.iscoroutine(configured):
                configured.close()
        return await self._prompt_input_reader(prompt)

    def _read_session_message_count(self) -> int:
        return len(self.session.entries)

    def _message_count(self) -> int:
        state_count = self._read_session_message_count()
        return state_count if state_count else self._fallback_message_count

    def _render_header(self) -> None:
        render_header(
            self.console,
            agent_id=self.agent_id,
            commands_line=self._slash_registry.command_line,
        )

    def _render_system_message(self, text: str, *, style: str = "cyan") -> None:
        render_system(self.console, text, style=style)

    def _render_error_message(self, text: str) -> None:
        render_error(self.console, text)

    def _attach_host_call_handlers(self) -> None:
        for lease in self._host_call_handler_leases:
            lease.close()
        handlers = CliHostCallHandlers(
            input_reader=self.input_reader,
            render_message=self._render_system_message,
        )
        self._host_call_handler_leases = list(
            handlers.attach(self._conversation.runtime_bus)
        )

    def _render_help(self) -> None:
        width = max(len(item.usage) for item in self._slash_registry.list()) + 2
        lines = ["[bold]Available commands[/bold]"]
        lines.extend(
            f"{item.usage:<{width}}{item.summary}"
            for item in self._slash_registry.list()
        )
        self.console.print(Text.from_markup("\n".join(lines)))

    def _render_session_summary(self) -> None:
        preview = self._conversation.snapshot()
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
        self.console.print(summary)

    def _close_session(self) -> None:
        self._conversation.archive()

    def _close_trace_sink(self) -> None:
        """兼容旧调用点；观察资源实际由 RuntimeConversation 持有。"""
        self._conversation._close_trace()

    def _list_available_models(self) -> list[str]:
        if self._host is not None:
            return [item.model_id for item in self._host.list_models()]
        if self._app_config is None:
            return []
        return [
            f"{provider}/{model}"
            for provider in sorted(self._app_config.providers)
            for model in sorted(self._app_config.providers[provider].models)
        ]

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
            selection = self._conversation.set_model(arg)
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
        try:
            self._conversation.set_thinking(level)
            self._render_system_message(f"thinking set to {level}")
        except ValueError as exc:
            self._render_error_message(str(exc))

    def _handle_agent_command(self, arg: str | None) -> None:
        if self._host is None:
            self._render_error_message("Boot/AppConfig 未提供，无法使用 /agent")
            return
        if not arg:
            agent_ids = [item.agent_id for item in self._host.list_agents()]
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
        try:
            self._conversation = self._host.switch_agent(
                self._conversation,
                agent_id,
            )
            self._attach_host_call_handlers()
            self._fallback_message_count = 0
            self._render_system_message(
                f"Switched to {agent_id}, new session {self.session.session_id}"
            )
        except Exception as exc:  # noqa: BLE001
            self._render_error_message(f"切换 agent 失败: {exc}")

    def _handle_new_command(self) -> None:
        if self._host is not None:
            self._conversation = self._host.new_session(self._conversation)
        else:
            session = Session.create(
                agent_id=self.agent_id,
                cwd=str(Path.cwd().resolve()),
            )
            service = self._session_service
            if service is not None:
                session = service.start(
                    agent_id=self.agent_id,
                    cwd=str(Path.cwd().resolve()),
                )
            self._conversation.detach()
            self._conversation = RuntimeConversation(
                agent=self.agent,
                run=self._run,
                session=session,
                session_service=service,
                app_config=self._app_config,
                trace_path_resolver=trace_path,
            )
        self._attach_host_call_handlers()
        self._fallback_message_count = 0
        self._render_system_message(
            f"New session {self.session.session_id} (agent={self.agent_id})"
        )

    async def _handle_reload_command(self) -> None:
        if self._run is None or self._host is None:
            self._render_error_message("Run 未提供")
            return
        try:
            app_config = Config.load(cwd=Path.cwd())
            result = await self._host.reload(
                self._conversation,
                app_config=app_config,
                boot_factory=Boot.from_config,
            )
            self._conversation = result.conversation
            self._attach_host_call_handlers()
            for warning in result.warnings:
                self._render_error_message(f"Extension load error: {warning}")
            self._render_system_message(
                "Reloaded skills, templates, settings, models, agent, auth. "
                f"Session={self.session.session_id} agent={self.agent_id}. "
                "Next turn uses new snapshot."
            )
        except Exception as exc:  # noqa: BLE001 — 失败保持旧快照
            self._render_error_message(f"reload 失败，保持旧配置: {exc}")

    async def _handle_command(self, user_input: str) -> bool:
        parsed = parse_slash(user_input)
        command = self._slash_registry.get(parsed.name)
        if command is None:
            self._render_error_message(f"Unknown command: {user_input}. Try /help.")
            return True
        handler = getattr(self, command.handler)
        outcome = handler(parsed.argument)
        if inspect.isawaitable(outcome):
            outcome = await outcome
        return bool(outcome)

    def complete(self, kind: str, argument: str) -> tuple[str, ...]:
        """SlashCompleter 的动态真源。"""
        if kind == "models":
            return tuple(self._list_available_models())
        if kind == "agents":
            return (
                tuple(item.agent_id for item in self._host.list_agents())
                if self._host is not None
                else ()
            )
        if kind == "thinking":
            return ("off", "low", "medium", "high", "xhigh")
        if kind == "tools":
            try:
                return tuple(item.name for item in self._conversation.list_tools())
            except AttributeError:
                return ()
        if kind == "skills":
            parts = argument.split()
            if len(parts) <= 1 and not argument.endswith(" "):
                return ("pending", "diff", "approve", "reject")
            action = parts[0].lower() if parts else ""
            if action in {"diff", "approve", "reject"}:
                return tuple(
                    item.pending_id for item in self._conversation.list_pending_skills()
                )
        return ()

    def _command_help(self, _arg: str | None) -> bool:
        self._render_help()
        return True

    def _command_model(self, arg: str | None) -> bool:
        self._handle_model_command(arg)
        return True

    def _command_thinking(self, arg: str | None) -> bool:
        self._handle_thinking_command(arg)
        return True

    def _command_agent(self, arg: str | None) -> bool:
        self._handle_agent_command(arg)
        return True

    def _command_new(self, _arg: str | None) -> bool:
        self._handle_new_command()
        return True

    async def _command_reload(self, _arg: str | None) -> bool:
        await self._handle_reload_command()
        return True

    async def _command_context(self, _arg: str | None) -> bool:
        await self._render_context_command()
        return True

    def _command_session(self, _arg: str | None) -> bool:
        self._render_session_summary()
        return True

    def _command_skills(self, arg: str | None) -> bool:
        self._handle_skills_command(arg)
        return True

    def _command_tools(self, arg: str | None) -> bool:
        self._render_tools(arg)
        return True

    def _command_clear(self, _arg: str | None) -> bool:
        self.console.clear(home=True)
        self._render_header()
        return True

    def _command_exit(self, _arg: str | None) -> bool:
        self._close_session()
        self._render_system_message("Session closed.")
        return False

    def _handle_skills_command(self, arg: str | None) -> None:
        conversation = getattr(self, "_conversation", None)
        store = getattr(self._run, "skill_store", None)
        parts = (arg or "pending").split(maxsplit=1)
        action = parts[0].lower()
        pending_id = parts[1].strip() if len(parts) > 1 else None

        if action == "pending":
            records = (
                conversation.list_pending_skills()
                if conversation is not None
                else tuple(store.list_pending()) if store is not None else ()
            )
            if not records:
                if getattr(self._run, "skill_store", None) is None:
                    self._render_error_message("当前 agent 未配置 skills 目录")
                else:
                    self._render_system_message("没有待审的 skill 写入")
                return
            # box=None：E3 无边框排版，列对齐保留、框线不画
            table = Table(title="Pending skill writes", title_justify="left", box=None)
            table.add_column("id")
            table.add_column("action")
            table.add_column("skill")
            table.add_column("agent")
            for record in records:
                table.add_row(
                    record.pending_id, record.action, record.skill_name, record.agent_id
                )
            self.console.print(table)
            return

        if pending_id is None:
            self._render_error_message(f"用法：/skills {action} <id>")
            return

        try:
            result = (
                conversation.apply_skill_action(action, pending_id)
                if conversation is not None
                else None
            )
            if action == "diff":
                self._render_system_message(f"diff {pending_id}")
                diff = result.diff if result is not None else store.diff(pending_id)
                self.console.print(Text(diff or ""))
                return
            if action == "approve":
                path = result.path if result is not None else store.approve(pending_id)
                self._render_system_message(f"已批准，写入 {path}（下一轮对话生效）")
                return
            if action == "reject":
                if result is None:
                    store.reject(pending_id)
                self._render_system_message(f"已拒绝 {pending_id}")
                return
        except SkillStoreError as exc:
            self._render_error_message(str(exc))
            return

        self._render_error_message(
            f"未知子命令：{action}。可用：pending / diff <id> / approve <id> / reject <id>"
        )

    def _render_tools(self, filter_text: str | None) -> None:
        try:
            tools = self._conversation.list_tools()
        except AttributeError:
            self._render_error_message("当前 Run 不支持工具快照")
            return
        if filter_text:
            needle = filter_text.lower()
            tools = tuple(item for item in tools if needle in item.name.lower())
        if not tools:
            self._render_system_message("当前没有激活的工具。")
            return
        table = Table(title="Active tools", title_justify="left", box=None)
        table.add_column("name")
        table.add_column("source")
        table.add_column("origin")
        for item in tools:
            table.add_row(item.name, item.source, item.origin or "-")
        self.console.print(table)

    async def _render_context_command(self) -> None:
        """`/context` = ContextUsage 视图（设计 §7）。

        展示「若现在进入下一 step，prepare 将发出的 Request」的估计占用，
        外加从 Session 派生的真实 API usage。只读：不跑 hook、不执行 recall、
        不写 Session。
        """
        inspection = await self._conversation.inspect_context()
        renderable = self._context_renderer.render(
            inspection.usage,
            last_turn=inspection.last_turn,
            session_total=inspection.session_total,
            note=inspection.note,
            source_line=(
                "Source: prepare preview · hooks skipped · recall skipped · no draft input"
            ),
            turns=inspection.turns,
            tool_calls=inspection.tool_calls,
            compactions=inspection.compactions,
            tool_definitions=inspection.tool_definitions,
        )
        self.console.print(renderable)

    async def run(self) -> None:
        # turn 中途的 KeyboardInterrupt 不经过下面任何 _close_session，
        # 故 trace 句柄在此兜底释放；_render_header 也在保护范围内。
        try:
            self._render_header()
            await self._loop()
        finally:
            self._close_trace_sink()
            if self._host is not None:
                await self._host.shutdown()

    async def _loop(self) -> None:
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
            bus, _event_renderer, unsubscribe_renderer = self.create_event_bus()
            task = asyncio.create_task(self.handle_user_input(user_input, bus=bus))
            try:
                await task
                self._conversation.flush()
            except KeyboardInterrupt:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, KeyboardInterrupt):
                    pass
                # 中断时 react 已补齐 tool_result 并落盘，这里再 flush 一次
                self._conversation.flush()
                continue
            except asyncio.CancelledError:
                continue
            except Exception:
                self._render_error_message(traceback.format_exc().rstrip())
                continue
            finally:
                unsubscribe_renderer()

            self._fallback_message_count += 1
            # last usage 由 /context 从 Session 派生（§11.8），不在此缓存
            # 渲染唯一入口是事件订阅（E3）：不发 AssistantMessageEvent 的
            # Run 是 runtime 违约，这里不做 fallback 渲染
