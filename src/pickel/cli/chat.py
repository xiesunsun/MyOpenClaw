from __future__ import annotations

import asyncio
import inspect
import logging
import traceback
from pathlib import Path
from typing import TYPE_CHECKING, Awaitable, Callable

from pickel.app.boot import Boot
from pickel.cli.context_renderer import ContextRenderer
from pickel.config.app_config import AppConfig
from pickel.config.loader import Config
from pickel.context.prepare import prepare
from pickel.conversations.service import SessionService
from pickel.extensions_host.loader import (
    LoadResult,
    load_extensions_async,
    teardown_extensions,
)
from pickel.conversations.session_storage_mapper import build_session_preview
from pickel.conversations.agent_message import (
    AssistantMessage,
    UserMessage,
    agent_message_from_dict,
)
from pickel.conversations.content_blocks import ToolCallContent
from pickel.conversations.session import Session
from pickel.conversations.session_entry import (
    ENTRY_TYPE_COMPACTION,
    ENTRY_TYPE_MESSAGE,
)
from pickel.skills.store import SkillStoreError
from pickel.cli.event_renderer import ChatEventRenderer
from pickel.cli.prompt_input import PromptToolkitInputReader
from pickel.cli.render.message import render_error, render_header, render_system
from pickel.runs.event_bus import EventBus
from pickel.runs.measure import measure
from pickel.runs.trace_sink import JsonlTraceSink, trace_enabled, trace_path
from pickel.runs.run import Run
from pickel.runs.turn_usage import last_turn_usage, session_usage
from pickel.runs.usage_anchor import resolve_anchor
from pickel.shared.model_config import ModelSelection
from rich.console import Console
from rich.table import Table
from rich.text import Text

if TYPE_CHECKING:
    from pickel.agents.agent import Agent

logger = logging.getLogger(__name__)


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
        # 进程级工具总线：跨 /reload 存活，由 Boot 持有的那一个
        self._tool_bus = boot.tool_bus if boot is not None else None
        self._extension_result = (
            getattr(boot, "extension_result", None) or LoadResult()
        )
        # bus 与 ChatLoop 同生命周期，绝不每轮重建：seq 由 bus 单调分配，
        # 每轮换 bus 会让 seq 回到 0，同一 session 的事件按 seq 排序就交错了（红线 4）。
        self._bus = EventBus()
        self._trace_sink: JsonlTraceSink | None = None
        self._unsubscribe_trace: Callable[[], None] | None = None
        self._open_trace_sink()

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
        bus: "EventBus | None" = None,
    ) -> AssistantMessage:
        if self._run is None:
            raise ValueError("Run 未提供")
        return await self._run.turn(
            session=self.session, user_text=text, bus=bus
        )

    def create_event_bus(self) -> tuple[EventBus, ChatEventRenderer, Callable[[], None]]:
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
        unsubscribe = self._bus.subscribe(renderer.handle_event)
        return self._bus, renderer, unsubscribe

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
        render_header(
            self.console,
            agent_id=self.agent_id,
            commands_line=(
                "/help  /model  /thinking  /agent  /new  /reload  /context  /session"
                "  /skills  /clear  /exit"
            ),
        )

    def _render_system_message(self, text: str, *, style: str = "cyan") -> None:
        render_system(self.console, text, style=style)

    def _render_error_message(self, text: str) -> None:
        render_error(self.console, text)

    def _render_help(self) -> None:
        help_text = Text.from_markup(
            "[bold]Available commands[/bold]\n"
            "/help              Show this help message\n"
            "/model [p/m]       List or set provider/model (Environ)\n"
            "/thinking <level>  Set thinking level in Environ\n"
            "/agent [id]        List agents or switch (new empty Session)\n"
            "/new               New empty Session, same agent\n"
            "/reload            Reload disk config/skills/agent (keep Environ and tool bus)\n"
            "/context           Show context usage (preview) and API usage\n"
            "/session           Show current session details\n"
            "/skills            Review agent skill writes: pending | diff <id> | approve <id> | reject <id>\n"
            "/clear             Clear the screen and redraw the header\n"
            "/exit              Exit the chat loop"
        )
        self.console.print(help_text)

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
        self.console.print(summary)

    def _close_session(self) -> None:
        self._close_trace_sink()
        if self._session_closed:
            return
        if self._session_service is not None:
            self._session_service.close(session=self.session)
        self._session_closed = True

    def _open_trace_sink(self) -> None:
        """按当前 session 建 trace sink 并挂上 bus；每次换 session 都要重调。

        文件名绑的是 session_id，不重建的话 /agent、/new 之后新 session 的事件
        会写进旧 session 的文件里。
        """
        self._close_trace_sink()
        if not trace_enabled(
            self._app_config.trace_enabled if self._app_config is not None else False
        ):
            return
        try:
            self._trace_sink = JsonlTraceSink(trace_path(self.session.session_id))
        except OSError as exc:  # 可观测性组件不得弄挂主流程（红线 5）
            self._trace_sink = None
            logger.warning("trace 打开失败，本次运行禁用 trace: %s", exc)
            return
        self._unsubscribe_trace = self._bus.subscribe(self._trace_sink)

    def _close_trace_sink(self) -> None:
        """trace 文件句柄的唯一释放点；幂等，异常路径由 run() 的 finally 兜底。"""
        if self._unsubscribe_trace is not None:
            self._unsubscribe_trace()
            self._unsubscribe_trace = None
        if self._trace_sink is not None:
            self._trace_sink.close()
            self._trace_sink = None

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
            self._open_trace_sink()
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
        self._open_trace_sink()
        self._render_system_message(
            f"New session {session.session_id} (agent={agent_id})"
        )

    async def _handle_reload_command(self) -> None:
        if self._run is None:
            self._render_error_message("Run 未提供")
            return
        try:
            app_config = Config.load(cwd=Path.cwd())
            # 复用同一个进程级 bus：reload 不该杀掉非内置来源的工具
            # （T2 的 MCP 子进程）；extension 则先卸后装，磁盘改动即时生效
            extensions = None
            if self._tool_bus is not None:
                await teardown_extensions(
                    self._extension_result, tool_bus=self._tool_bus
                )
                self._extension_result = await load_extensions_async(
                    tool_bus=self._tool_bus,
                    app_config=app_config,
                )
                for error in self._extension_result.errors:
                    self._render_error_message(f"Extension load error: {error}")
                extensions = self._extension_result.registry
            boot = Boot.from_config(
                app_config, tool_bus=self._tool_bus, extensions=extensions
            )
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
            self._tool_bus = boot.tool_bus
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
            await self._handle_reload_command()
            return True
        if command == "/context":
            await self._render_context_command()
            return True
        if command == "/session":
            self._render_session_summary()
            return True
        if command == "/skills":
            self._handle_skills_command(arg)
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

    def _handle_skills_command(self, arg: str | None) -> None:
        store = getattr(self._run, "skill_store", None) if self._run else None
        if store is None:
            self._render_error_message("当前 agent 未配置 skills 目录")
            return
        parts = (arg or "pending").split(maxsplit=1)
        action = parts[0].lower()
        pending_id = parts[1].strip() if len(parts) > 1 else None

        if action == "pending":
            records = store.list_pending()
            if not records:
                self._render_system_message("没有待审的 skill 写入")
                return
            # box=None：E3 无边框排版，列对齐保留、框线不画
            table = Table(
                title="Pending skill writes", title_justify="left", box=None
            )
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
            if action == "diff":
                self._render_system_message(f"diff {pending_id}")
                self.console.print(Text(store.diff(pending_id)))
                return
            if action == "approve":
                path = store.approve(pending_id)
                self._render_system_message(f"已批准，写入 {path}（下一轮对话生效）")
                return
            if action == "reject":
                store.reject(pending_id)
                self._render_system_message(f"已拒绝 {pending_id}")
                return
        except SkillStoreError as exc:
            self._render_error_message(str(exc))
            return

        self._render_error_message(
            f"未知子命令：{action}。可用：pending / diff <id> / approve <id> / reject <id>"
        )

    async def _render_context_command(self) -> None:
        """`/context` = ContextUsage 视图（设计 §7）。

        展示「若现在进入下一 step，prepare 将发出的 Request」的估计占用，
        外加从 Session 派生的真实 API usage。只读：不跑 hook、不执行 recall、
        不写 Session。
        """
        last_turn = last_turn_usage(self.session)
        total = session_usage(self.session)
        run = self._run
        turns, tool_calls, compactions = _session_context_stats(self.session)

        usage = None
        note = None
        tool_defs = 0
        if run is None:
            note = "尚无 Run，无法组装上下文"
        else:
            try:
                snapshot = run.tool_bus.snapshot(run.activation)
                tool_defs = len(snapshot.entries)
                request = await prepare(
                    run=run,
                    session=self.session,
                    hook_feedback=[],
                    unit_window=run.unit_window,
                    # §7.3：预览不得执行 recall（含远程 OV）
                    recall_sources=[],
                    # 预览按当前激活集现取一份快照（不在 turn 内，无缓存一致性顾虑）
                    snapshot=snapshot,
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
            turns=turns,
            tool_calls=tool_calls,
            compactions=compactions,
            tool_definitions=tool_defs,
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
                if self._session_service is not None:
                    self._session_service.flush_new_entries(
                        session=self.session,
                        entries=[],
                    )
            except KeyboardInterrupt:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, KeyboardInterrupt):
                    pass
                # 中断时 react 已补齐 tool_result 并落盘，这里再 flush 一次
                if self._session_service is not None:
                    self._session_service.flush_new_entries(
                        session=self.session,
                        entries=[],
                    )
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


def _session_context_stats(session: Session) -> tuple[int, int, int]:
    """从 Session active_path 统计 turns / tool_calls / compactions。"""
    turns = 0
    tool_calls = 0
    compactions = 0
    for entry in session.active_path():
        if entry.entry_type == ENTRY_TYPE_COMPACTION:
            compactions += 1
            continue
        if entry.entry_type != ENTRY_TYPE_MESSAGE:
            continue
        try:
            message = agent_message_from_dict(entry.payload)
        except (KeyError, TypeError, ValueError):
            continue
        if isinstance(message, UserMessage):
            turns += 1
        elif isinstance(message, AssistantMessage):
            tool_calls += sum(
                1
                for block in message.content
                if isinstance(block, ToolCallContent)
            )
    return turns, tool_calls, compactions
