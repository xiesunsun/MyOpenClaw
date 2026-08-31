from __future__ import annotations

import asyncio
import inspect
import traceback
import webbrowser
from pathlib import Path
from typing import Awaitable, Callable

from pickel.app.boot import Boot
from pickel.app.conversation_runtime import ConversationRuntime
from pickel.app.runtime_host import RuntimeHost
from pickel.app.runtime_models import (
    ConversationRequest,
    McpServerInfo,
    AgentRunRequest,
    AgentRunResult,
)
from pickel.cli.context_renderer import ContextRenderer
from pickel.cli.audio_output_handler import CliAudioOutputHandler
from pickel.cli.audio_player import MacAudioPlayer
from pickel.cli.host_call_handlers import CliHostCallHandlers
from pickel.cli.slash import (
    BUILTIN_SLASH_COMMANDS,
    SlashCompleter,
    parse_slash,
)
from pickel.config.loader import Config
from pickel.conversations.agent_message import UserMessage
from pickel.conversations.content_blocks import TextBlock
from pickel.conversations.conversation_session import ConversationSession
from pickel.skills.store import SkillStoreError
from pickel.cli.event_renderer import ChatEventRenderer
from pickel.cli.prompt_input import PromptToolkitInputReader
from pickel.cli.render.message import render_error, render_header, render_system
from pickel.runtime.event_bus import EventBus
from rich.console import Console
from rich.table import Table
from rich.text import Text


class ChatLoop:
    def __init__(
        self,
        *,
        conversation: ConversationRuntime,
        host: RuntimeHost | None = None,
        console: Console | None = None,
        input_reader: Callable[[str], str | Awaitable[str]] | None = None,
        context_renderer: ContextRenderer | None = None,
    ) -> None:
        self.console = console or Console()
        self._prompt_input_reader: PromptToolkitInputReader | None = None
        self.input_reader = input_reader or self._default_input_reader
        self._context_renderer = context_renderer or ContextRenderer()
        self._host = host
        self._conversation = conversation
        self._host_call_handler_leases = []
        self._attach_host_call_handlers()
        self._audio_output_unsubscribe: Callable[[], None] | None = None
        self._audio_output_handler: CliAudioOutputHandler | None = None
        self._attach_audio_output_handler()
        self._fallback_message_count = self._read_session_message_count()
        self._slash_registry = BUILTIN_SLASH_COMMANDS
        self._slash_completer = SlashCompleter(self._slash_registry, self)
        self._observation_server = None

    @classmethod
    def from_host(
        cls,
        *,
        host: RuntimeHost,
        agent_id: str | None = None,
        session_id: str | None = None,
    ) -> "ChatLoop":
        conversation = host.open_conversation(
            ConversationRequest(
                agent_id=agent_id,
                session_id=session_id,
                cwd=Path.cwd(),
                mode="interactive",
            )
        )
        return cls(
            host=host,
            conversation=conversation,
        )

    # Surface 只读取 Conversation 状态，不持有第二份副本。
    @property
    def agent_definition(self):
        return self._conversation.agent_definition

    @property
    def agent_id(self) -> str:
        return self.agent_definition.agent_id

    @property
    def session(self) -> ConversationSession:
        return self._conversation.session

    @property
    def _boot(self) -> Boot | None:
        return self._host.boot if self._host is not None else None

    @property
    def _tool_bus(self):
        return self._boot.tool_bus if self._boot is not None else None

    @property
    def _bus(self) -> EventBus:
        return self._conversation.event_bus

    async def handle_user_input(
        self,
        text: str,
    ) -> AgentRunResult:
        return await self._conversation.start_agent_run(
            AgentRunRequest(
                message=UserMessage(content=[TextBlock(text=text)]),
            )
        )

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
            # usage=None 时 footer 退到这个 label。
            fallback_model_label=(
                f"{self._conversation.model_config.provider} / "
                f"{self._conversation.model_config.model}"
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
        return self._conversation.snapshot().message_count

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

    def _attach_audio_output_handler(self) -> None:
        if self._audio_output_unsubscribe is not None:
            self._audio_output_unsubscribe()
        if self._audio_output_handler is not None:
            self._audio_output_handler.close()
        self._audio_output_handler = CliAudioOutputHandler(
            player=MacAudioPlayer(),
            render_error=self._render_error_message,
        )
        self._audio_output_unsubscribe = self._conversation.subscribe_outputs(
            self._audio_output_handler.handle_output
        )

    def _close_audio_output_handler(self) -> None:
        if self._audio_output_unsubscribe is not None:
            self._audio_output_unsubscribe()
            self._audio_output_unsubscribe = None
        if self._audio_output_handler is not None:
            self._audio_output_handler.close()
            self._audio_output_handler = None

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
        collaboration = self._conversation.collaboration
        summary = Text(
            "\n".join(
                [
                    f"Session ID: {preview.session_id}",
                    f"Agent: {preview.agent_id}",
                    f"Status: {preview.status}",
                    f"Messages: {preview.message_count}",
                    f"Updated: {preview.updated_at.isoformat()}",
                    f"Last message: {preview.last_message or '-'}",
                    f"Collaboration: {collaboration.mode}",
                    f"Goal: {collaboration.goal or '-'}",
                ]
            ),
        )
        self.console.print(summary)

    def _command_plan(self, arg: str | None) -> bool:
        argument = (arg or "").strip()
        if argument.lower() == "off":
            self._conversation.set_collaboration_mode("normal")
            self._render_system_message("已退出 Plan 模式。")
            return True
        plan = (argument,) if argument else ()
        self._conversation.set_collaboration_mode("plan", plan=plan)
        self._render_system_message(
            "已进入 Plan 模式：模型只能使用 ls/glob/grep/read，完成计划后不会修改文件。"
        )
        return True

    def _command_goal(self, arg: str | None) -> bool:
        argument = (arg or "").strip()
        if argument.lower() == "off":
            self._conversation.set_collaboration_mode("normal")
            self._render_system_message("已退出 Goal 模式。")
            return True
        if not argument:
            self._render_error_message("用法：/goal <goal> 或 /goal off")
            return True
        self._conversation.set_collaboration_mode("goal", goal=argument)
        self._render_system_message(f"已进入 Goal 模式：{argument}")
        return True

    def _close_session(self) -> None:
        self._conversation.archive()

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
            self._close_observation_server()
            self._attach_host_call_handlers()
            self._attach_audio_output_handler()
            self._fallback_message_count = 0
            self._render_system_message(
                f"Switched to {agent_id}, new session {self.session.session_id}"
            )
        except Exception as exc:  # noqa: BLE001
            self._render_error_message(f"切换 agent 失败: {exc}")

    def _handle_new_command(self) -> None:
        if self._host is None:
            self._render_error_message("RuntimeHost 未提供，无法创建新会话")
            return
        self._conversation = self._host.new_session(self._conversation)
        self._close_observation_server()
        self._attach_host_call_handlers()
        self._attach_audio_output_handler()
        self._fallback_message_count = 0
        self._render_system_message(
            f"New session {self.session.session_id} (agent={self.agent_id})"
        )

    async def _handle_reload_command(self) -> None:
        if self._host is None:
            self._render_error_message("RuntimeHost 未提供")
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
            self._attach_audio_output_handler()
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
        if kind == "agents":
            return (
                tuple(item.agent_id for item in self._host.list_agents())
                if self._host is not None
                else ()
            )
        if kind == "tools":
            try:
                return tuple(item.name for item in self._conversation.list_tools())
            except AttributeError:
                return ()
        if kind == "mcp_servers":
            if self._host is None:
                return ()
            return tuple(
                item.name for item in self._host.inspect_mcp(self._conversation).servers
            )
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

    async def _command_compact(self, _arg: str | None) -> bool:
        result = await self._conversation.compact_history()
        if result.succeeded:
            self._render_system_message(result.message)
        else:
            self._render_error_message(f"{result.code}: {result.message}")
        return True

    def _command_observe(self, arg: str | None) -> bool:
        argument = (arg or "").strip()
        if argument == "export" or argument.startswith("export "):
            raw_path = argument.removeprefix("export").strip()
            try:
                path = self._conversation.export_observation(
                    Path(raw_path).expanduser() if raw_path else None
                )
            except (OSError, ValueError) as exc:
                self._render_error_message(f"导出观测报告失败: {exc}")
                return True
            self._render_system_message(f"Observation report: {path}")
            return True

        try:
            port = int(argument) if argument else 0
        except ValueError:
            self._render_error_message(
                "用法：/observe [port] 或 /observe export [path]"
            )
            return True
        if not 0 <= port <= 65535:
            self._render_error_message("观测端口必须在 0 到 65535 之间")
            return True

        session_id = self.session.session_id
        if (
            self._observation_server is not None
            and self._observation_server.session_id == session_id
        ):
            webbrowser.open(self._observation_server.url)
            self._render_system_message(
                f"Observation workspace: {self._observation_server.url}"
            )
            return True

        self._close_observation_server()
        try:
            from pickel.observe.http_server import start_observation_server

            store = self._conversation.persistence_store
            self._observation_server = start_observation_server(
                store=store,
                content_store=store.model_call_content_store,
                session_id=session_id,
                port=port,
            )
        except (OSError, ValueError) as exc:
            self._render_error_message(f"启动动态观测站点失败: {exc}")
            return True
        webbrowser.open(self._observation_server.url)
        self._render_system_message(
            f"Observation workspace: {self._observation_server.url}"
        )
        return True

    def _close_observation_server(self) -> None:
        if self._observation_server is None:
            return
        self._observation_server.close()
        self._observation_server = None

    def _command_session(self, _arg: str | None) -> bool:
        self._render_session_summary()
        return True

    def _command_skills(self, arg: str | None) -> bool:
        self._handle_skills_command(arg)
        return True

    def _command_tools(self, arg: str | None) -> bool:
        self._render_tools(arg)
        return True

    def _command_mcp(self, arg: str | None) -> bool:
        self._render_mcp(arg)
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
        parts = (arg or "pending").split(maxsplit=1)
        action = parts[0].lower()
        pending_id = parts[1].strip() if len(parts) > 1 else None

        if action == "pending":
            records = (
                conversation.list_pending_skills() if conversation is not None else ()
            )
            if not records:
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
                diff = result.diff if result is not None else None
                self.console.print(Text(diff or ""))
                return
            if action == "approve":
                path = result.path if result is not None else None
                self._render_system_message(f"已批准，写入 {path}（下一轮对话生效）")
                return
            if action == "reject":
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

    def _render_mcp(self, server_name: str | None) -> None:
        if self._host is None:
            self._render_error_message("当前 Runtime 不支持 MCP 状态查询")
            return

        inspection = self._host.inspect_mcp(self._conversation)
        if not inspection.available:
            self._render_system_message("MCP extension is disabled or unavailable.")
            return

        servers = inspection.servers
        if server_name is not None:
            server = next((item for item in servers if item.name == server_name), None)
            if server is None:
                self._render_error_message(f"Unknown MCP server: {server_name}")
            else:
                self._render_mcp_server(server)
            self._render_mcp_diagnostics(inspection.diagnostics)
            return

        if not servers:
            message = (
                "No MCP servers available."
                if inspection.diagnostics
                else "No MCP servers configured."
            )
            self._render_system_message(message)
            self._render_mcp_diagnostics(inspection.diagnostics)
            return

        table = Table(title="MCP servers", title_justify="left", box=None)
        table.add_column("server")
        table.add_column("status")
        table.add_column("transport")
        table.add_column("tools")
        table.add_column("protocol")
        for server in servers:
            table.add_row(
                server.name,
                server.status,
                server.transport,
                f"{server.discovered_tools} / {server.active_tools}",
                server.protocol_version or "-",
            )
        table.caption = "tools = discovered / active"
        self.console.print(table)
        self._render_mcp_diagnostics(inspection.diagnostics)

    def _render_mcp_server(self, server: McpServerInfo) -> None:
        table = Table(
            title=f"MCP server: {server.name}", title_justify="left", box=None
        )
        table.add_column("field")
        table.add_column("value")
        rows = (
            ("Status", server.status),
            ("Transport", server.transport),
            ("Config", server.config_scope or "-"),
            ("Implementation", server.implementation or "-"),
            ("Protocol", server.protocol_version or "-"),
            (
                "Tools",
                f"{server.discovered_tools} discovered / {server.active_tools} active",
            ),
        )
        for label, value in rows:
            table.add_row(label, value)
        if server.last_error:
            table.add_row("Error", server.last_error)
        self.console.print(table)

    def _render_mcp_diagnostics(self, diagnostics: tuple[str, ...]) -> None:
        if not diagnostics:
            return
        self.console.print(Text("Diagnostics", style="bold"))
        for diagnostic in diagnostics:
            self._render_system_message(diagnostic)

    async def _render_context_command(self) -> None:
        """`/context` = ContextUsage 视图（设计 §7）。

        展示「若现在进入下一 ModelStep，将发出的 Request」的估计占用，
        外加从 Session 派生的真实 API usage。只读：不跑 hook、不执行 recall、
        不写 Session。
        """
        try:
            inspection = await self._conversation.inspect_context()
        except Exception as exc:
            # 诊断命令失败不能退出交互循环；具体错误仍展示给用户，便于定位。
            self._render_error_message(f"读取 Context 失败: {exc}")
            return
        renderable = self._context_renderer.render(
            inspection.usage,
            last_turn=inspection.last_turn,
            session_total=inspection.session_total,
            note=inspection.note,
            source_line=(
                (
                    "Source: committed ModelRequestIntent · exact Provider-neutral context"
                )
                if inspection.source == "model_request_intent"
                else "Source: context preview · hooks skipped · recall skipped · no draft input"
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
            self._close_observation_server()
            self._close_audio_output_handler()
            if not self._conversation.closed:
                self._conversation.detach()

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
            _bus, _event_renderer, unsubscribe_renderer = self.create_event_bus()
            task = asyncio.create_task(self.handle_user_input(user_input))
            try:
                result = await task
                if result.status == "failed":
                    error = result.error
                    self._render_error_message(
                        f"{error.error_type}: {error.message}"
                        if error is not None
                        else "Runtime 执行失败"
                    )
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
