import json
import os
import unittest
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
import textwrap
from unittest.mock import AsyncMock, Mock, patch

from pickel.agents.agent import Agent
from pickel.cli.context_renderer import ContextRenderer
from pickel.context.assembler import ContextAssembler
from pickel.conversations.agent_message import AssistantMessage, UserMessage
from pickel.conversations.content_blocks import TextContent, ToolCallContent
from pickel.hooks.lifecycle import NoopLifecycleHooks
from pickel.runs import ReActStrategy, Run
from pickel.tools.shell import ShellSessionManager
from pickel.conversations.metadata import MessageMetadata
from pickel.conversations.session import Session
from pickel.conversations.session_entry import SessionEntry
from pickel.conversations.session_preview import SessionPreview
from pickel.cli.chat import ChatLoop
from pickel.conversations.agent_message import ModelResponseMetadata, ModelUsage
from pickel.runs.usage_anchor import context_fingerprint
from pickel.runs.event_bus import EventBus
from pickel.runs.runtime_events import (
    AssistantMessageEvent,
    StepStarted,
    ToolCallCompleted,
    ToolCallStarted,
)
from pickel.runs.turn_usage import TurnUsage
from pickel.shared.event_envelope import EventEnvelope
from pickel.conversations.message import ToolCall
from pickel.shared.model_config import ModelConfig
from pickel.tools.base import ToolExecutionResult
from pickel.tools.bus import ToolActivation, bus_with
from rich.console import Console
from rich.text import Text


def _assistant_text(message: AssistantMessage) -> str:
    return "\n".join(
        block.text for block in message.content if isinstance(block, TextContent) and block.text
    )


def _model_metadata() -> ModelResponseMetadata:
    return ModelResponseMetadata(
        provider="google/gemini",
        model="gemini-3-flash-preview",
        elapsed_ms=100,
        usage=ModelUsage(input_tokens=100, output_tokens=10),
    )


def _text_assistant(text: str, *, metadata: MessageMetadata | None = None) -> AssistantMessage:
    model_meta = None
    if metadata is not None:
        from pickel.conversations.agent_message import ModelResponseMetadata

        model_meta = ModelResponseMetadata(
            provider=metadata.provider or "fake",
            model=metadata.model or "fake",
            elapsed_ms=metadata.elapsed_ms,
            finish_reason=metadata.provider_finish_reason,
            finish_message=metadata.provider_finish_message,
            provider_response_id=metadata.provider_response_id,
            provider_model_version=metadata.provider_model_version,
        )
    return AssistantMessage(content=[TextContent(text=text)], metadata=model_meta)


class StubRun:
    async def turn(
        self,
        *,
        session: Session,
        user_text: str,
        bus: EventBus | None = None,
    ) -> AssistantMessage:
        session.append_user(UserMessage(content=[TextContent(text=user_text)]))
        reply = _text_assistant("runtime reply")
        session.append_assistant(reply)
        if bus is not None:
            # 真 Run.turn 会填 session_id/turn_id，桩照做，否则 trace 路由测不出问题
            await bus.emit(
                AssistantMessageEvent(
                    envelope=EventEnvelope(session_id=session.session_id),
                    text="runtime reply",
                )
            )
        return reply


class SilentRun:
    async def turn(
        self,
        *,
        session: Session,
        user_text: str,
        bus: EventBus | None = None,
    ) -> AssistantMessage:
        session.append_user(UserMessage(content=[TextContent(text=user_text)]))
        reply = _text_assistant("runtime reply")
        session.append_assistant(reply)
        return reply


class ErrorRun:
    async def turn(
        self,
        *,
        session: Session,
        user_text: str,
        bus: EventBus | None = None,
    ) -> AssistantMessage:
        session.append_user(UserMessage(content=[TextContent(text=user_text)]))
        # 真 Run 在失败前也已经发过事件；桩照做，否则「失败轮不退订渲染器」
        # 这种回归在后续轮次里没有任何可观测痕迹
        if bus is not None:
            await bus.emit(StepStarted(envelope=EventEnvelope(step_index=1)))
        raise ValueError("boom")


class InterruptRun:
    async def turn(
        self,
        *,
        session: Session,
        user_text: str,
        bus: EventBus | None = None,
    ) -> AssistantMessage:
        raise KeyboardInterrupt


class StubToolRun:
    async def turn(
        self,
        *,
        session: Session,
        user_text: str,
        bus: EventBus | None = None,
    ) -> AssistantMessage:
        session.append_user(UserMessage(content=[TextContent(text=user_text)]))
        if bus is not None:
            await bus.emit(StepStarted(envelope=EventEnvelope(step_index=1)))
        tool_call = ToolCall(
            id="call-1",
            name="read_file",
            arguments={"path": "/tmp/" + "very-long-segment/" * 12 + "file.txt"},
        )
        if bus is not None:
            await bus.emit(
                ToolCallStarted(
                    envelope=EventEnvelope(step_index=1),
                    batch_id="batch-1",
                    call_index=0,
                    total_calls=1,
                    tool_call=tool_call,
                )
            )
            await bus.emit(
                ToolCallCompleted(
                    envelope=EventEnvelope(step_index=1),
                    batch_id="batch-1",
                    call_index=0,
                    total_calls=1,
                    tool_call=tool_call,
                    tool_result=ToolExecutionResult(
                        content="file content " * 80,
                        metadata={
                            "cwd": "/tmp/workspace",
                            "exit_code": 0,
                            "shell_status": "ready",
                        },
                    ),
                )
            )
            await bus.emit(
                AssistantMessageEvent(
                    envelope=EventEnvelope(step_index=1),
                    text="final reply",
                    usage=TurnUsage(
                        steps=1,
                        input_tokens=11,
                        output_tokens=7,
                        model_label="google/gemini / gemini-3-flash-preview",
                    ),
                )
            )
        return _text_assistant(
            "final reply",
            metadata=MessageMetadata(
                provider="google/gemini",
                model="gemini-3-flash-preview",
            ),
        )


class StubContextRun:
    def __init__(self, agent: Agent) -> None:
        self.agent = agent
        self.provider = Mock()
        self.tool_bus = bus_with([])
        self.activation = ToolActivation(allowed=frozenset())
        self.unit_window = 5
        self.context_assembler = Mock()
        self.strategy = Mock()

    async def turn(
        self,
        *,
        session: Session,
        user_text: str,
        bus: EventBus | None = None,
    ) -> AssistantMessage:
        raise AssertionError("turn should not be called")


class ExplodingRecall:
    """/context 若执行 recall 就会炸（§7.3 预览不得执行 recall）。"""

    def __init__(self) -> None:
        self.calls = 0

    async def provide(self, *, run, session, current_user_text=""):
        self.calls += 1
        raise AssertionError("/context 不得执行 recall")


class FakeSessionService:
    def __init__(self) -> None:
        self.flush_calls: list[list[str]] = []
        self.closed = False
        self.closed_sessions: list[Session] = []

    def build_preview(self, *, session: Session) -> SessionPreview:
        return SessionPreview(
            session_id=session.session_id,
            agent_id=session.agent_id,
            created_at=session.created_at,
            updated_at=session.updated_at,
            status=session.status,
            message_count=len(session.entries),
            last_message="runtime reply",
        )

    def flush_new_entries(self, *, session: Session, entries: list[SessionEntry]) -> None:
        self.flush_calls.append([entry.entry_id for entry in entries])

    def close(self, *, session: Session) -> None:
        self.closed = True
        self.closed_sessions.append(session)


class ChatLoopTests(unittest.IsolatedAsyncioTestCase):
    def _build_agent(self) -> Agent:
        return Agent(
            agent_id="Pickle",
            workspace_path=Path("/tmp/pickle"),
            behavior_path=Path("/tmp/pickle/AGENT.md"),
            behavior_instruction="You are Pickle.",
            model_config=ModelConfig(
                provider="google/gemini",
                model="gemini-3-flash-preview",
            ),
            tool_ids=[],
        )

    async def test_handle_user_input_delegates_to_coordinator_and_updates_session_count(self) -> None:
        agent = self._build_agent()
        session = Session.create(agent_id="Pickle", session_id="session-1")
        loop = ChatLoop(
            agent=agent,
            run=StubRun(),
            session=session,
        )

        result = await loop.handle_user_input("hello")

        self.assertEqual("runtime reply", _assistant_text(result))
        self.assertEqual(2, loop._message_count())

    async def test_chat_loop_creates_session_from_conversation_layer(self) -> None:
        agent = self._build_agent()

        loop = ChatLoop(
            agent=agent,
            run=StubRun(),
        )

        self.assertEqual("Pickle", loop.session.agent_id)

    async def test_handle_user_input_renders_tool_batch_progress_before_final_reply(self) -> None:
        agent = self._build_agent()
        session = Session.create(agent_id="Pickle", session_id="session-1")
        console = Mock()
        loop = ChatLoop(
            agent=agent,
            run=StubToolRun(),
            session=session,
            console=console,
        )

        bus, _, _unsubscribe = loop.create_event_bus()
        result = await loop.handle_user_input("hello", bus=bus)

        titles = [call.args[0].title for call in console.print.call_args_list]
        started_render = str(console.print.call_args_list[1].args[0].renderable)
        completed_render = str(console.print.call_args_list[2].args[0].renderable)

        self.assertEqual("final reply", _assistant_text(result))
        self.assertEqual(["Thinking", "Tool", "Tool", "Assistant"], titles)
        self.assertIn("read_file(path=", started_render)
        self.assertIn("status: running", started_render)
        self.assertNotIn("step:", started_render)
        self.assertIn("read_file(path=", completed_render)
        self.assertIn("status: ok", completed_render)
        self.assertIn("result: file content", completed_render)
        self.assertNotIn("meta:", completed_render)

    async def test_render_turn_output_replays_assistant_tool_batch(self) -> None:
        agent = self._build_agent()
        session = Session.create(agent_id="Pickle", session_id="session-1")
        from pickel.conversations.agent_message import ToolResultMessage

        session.append_tool_result(
            ToolResultMessage(
                tool_call_id="call-1",
                tool_name="read_file",
                content=[TextContent(text="hello world")],
            )
        )
        console = Mock()
        loop = ChatLoop(
            agent=agent,
            run=StubRun(),
            session=session,
            console=console,
        )

        loop.render_turn_output(
            _text_assistant(
                "final reply",
                metadata=MessageMetadata(provider="google/gemini", model="gemini-3-flash-preview"),
            ),
            start_index=0,
        )

        printed = [call.args[0] for call in console.print.call_args_list]
        self.assertTrue(any(isinstance(item, Text) and "read_file" in str(item) for item in printed))
        titles = [getattr(item, "title", None) for item in printed]
        self.assertIn("Assistant", titles)
        tool_line = next(str(item) for item in printed if isinstance(item, Text))
        self.assertIn("[read_file]", tool_line)
        self.assertIn("hello world", tool_line)

    @patch("pickel.cli.chat.PromptToolkitInputReader")
    async def test_chat_loop_uses_prompt_toolkit_reader_by_default(self, prompt_reader_cls: Mock) -> None:
        prompt_reader = AsyncMock(return_value="hello")
        prompt_reader_cls.return_value = prompt_reader

        loop = ChatLoop(
            agent=self._build_agent(),
            run=StubRun(),
        )

        self.assertEqual("hello", await loop.input_reader("You > "))
        prompt_reader_cls.assert_called_once_with()
        prompt_reader.assert_called_once_with("You > ")

    async def test_run_falls_back_to_render_final_reply_when_no_event_was_emitted(self) -> None:
        console = Mock()
        submitted_inputs = iter(["hello", "/exit"])
        loop = ChatLoop(
            agent=self._build_agent(),
            run=SilentRun(),
            session=Session.create(agent_id="Pickle", session_id="session-1"),
            console=console,
            input_reader=lambda _: next(submitted_inputs),
        )

        await loop.run()

        printed = [call.args[0] for call in console.print.call_args_list]
        titles = [getattr(renderable, "title", None) for renderable in printed]

        self.assertEqual(["MyOpenClaw Chat", "Assistant", "System"], titles)
        self.assertEqual("runtime reply", printed[1].renderable.markup)

    async def test_run_does_not_duplicate_final_reply_after_assistant_event(self) -> None:
        console = Mock()
        submitted_inputs = iter(["hello", "/exit"])
        loop = ChatLoop(
            agent=self._build_agent(),
            run=StubRun(),
            session=Session.create(agent_id="Pickle", session_id="session-1"),
            console=console,
            input_reader=lambda _: next(submitted_inputs),
        )

        await loop.run()

        titles = [getattr(call.args[0], "title", None) for call in console.print.call_args_list]
        self.assertEqual(1, titles.count("Assistant"))
        self.assertNotIn("You", titles)

    async def test_run_renders_full_traceback_when_turn_fails(self) -> None:
        output = StringIO()
        console = Console(file=output, force_terminal=False, width=120, record=True)
        submitted_inputs = iter(["hello", "/exit"])
        loop = ChatLoop(
            agent=self._build_agent(),
            run=ErrorRun(),
            session=Session.create(agent_id="Pickle", session_id="session-1"),
            console=console,
            input_reader=lambda _: next(submitted_inputs),
        )

        await loop.run()

        rendered = console.export_text()
        self.assertIn("Traceback (most recent call last):", rendered)
        self.assertIn("ValueError: boom", rendered)

    async def test_run_flushes_new_messages_after_turn(self) -> None:
        console = Mock()
        submitted_inputs = iter(["hello", "/exit"])
        session_service = FakeSessionService()
        loop = ChatLoop(
            agent=self._build_agent(),
            run=SilentRun(),
            session=Session.create(agent_id="Pickle", session_id="session-1"),
            console=console,
            input_reader=lambda _: next(submitted_inputs),
            session_service=session_service,
        )

        await loop.run()

        # ChatLoop post-turn flush is empty (run already checkpoint-flushed via session_service).
        self.assertEqual([[]], session_service.flush_calls)

    async def test_run_uses_existing_message_count_as_local_flush_start_index(self) -> None:
        console = Mock()
        submitted_inputs = iter(["hello", "/exit"])
        session_service = FakeSessionService()
        session = Session.create(agent_id="Pickle", session_id="session-1")
        session.append_user(UserMessage(content=[TextContent(text="previous")]))
        session.append_assistant(_text_assistant("old reply"))
        loop = ChatLoop(
            agent=self._build_agent(),
            run=SilentRun(),
            session=session,
            console=console,
            input_reader=lambda _: next(submitted_inputs),
            session_service=session_service,
        )

        await loop.run()

        self.assertEqual([[]], session_service.flush_calls)
        self.assertEqual(4, len(session.entries))

    async def test_run_closes_session_on_exit(self) -> None:
        console = Mock()
        submitted_inputs = iter(["/exit"])
        session_service = FakeSessionService()
        loop = ChatLoop(
            agent=self._build_agent(),
            run=SilentRun(),
            session=Session.create(agent_id="Pickle", session_id="session-1"),
            console=console,
            input_reader=lambda _: next(submitted_inputs),
            session_service=session_service,
        )

        await loop.run()

        self.assertTrue(session_service.closed)
        self.assertEqual("session-1", session_service.closed_sessions[0].session_id)

    async def test_trace_sink_receives_events_and_closes_on_exit(self) -> None:
        with TemporaryDirectory() as tmpdir:
            trace_file = Path(tmpdir) / "traces" / "session-1.jsonl"
            submitted_inputs = iter(["hello", "/exit"])
            with (
                patch.dict(os.environ, {"PICKEL_TRACE": "1"}),
                patch("pickel.cli.chat.trace_path", return_value=trace_file),
            ):
                loop = ChatLoop(
                    agent=self._build_agent(),
                    run=StubRun(),
                    session=Session.create(agent_id="Pickle", session_id="session-1"),
                    console=Mock(),
                    input_reader=lambda _: next(submitted_inputs),
                )
                sink = loop._trace_sink
                await loop.run()

            events = [
                json.loads(line)
                for line in trace_file.read_text(encoding="utf-8").splitlines()
            ]

        self.assertEqual(["assistant_message"], [e["event_type"] for e in events])
        self.assertIsNone(loop._trace_sink)
        self.assertTrue(sink.closed)

    async def test_trace_sink_closes_when_turn_raises_keyboard_interrupt(self) -> None:
        """Ctrl-C 打断 turn 不经过 _close_session，句柄由 run() 的 finally 释放。"""
        with TemporaryDirectory() as tmpdir:
            trace_file = Path(tmpdir) / "traces" / "session-1.jsonl"
            submitted_inputs = iter(["hello"])
            with (
                patch.dict(os.environ, {"PICKEL_TRACE": "1"}),
                patch("pickel.cli.chat.trace_path", return_value=trace_file),
            ):
                loop = ChatLoop(
                    agent=self._build_agent(),
                    run=InterruptRun(),
                    session=Session.create(agent_id="Pickle", session_id="session-1"),
                    console=Mock(),
                    input_reader=lambda _: next(submitted_inputs),
                )
                sink = loop._trace_sink
                with self.assertRaises(KeyboardInterrupt):
                    await loop.run()

        self.assertTrue(sink.closed)

    async def test_trace_disabled_by_default_builds_no_sink(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PICKEL_TRACE", None)
            loop = ChatLoop(
                agent=self._build_agent(),
                run=StubRun(),
                session=Session.create(agent_id="Pickle", session_id="session-1"),
                console=Mock(),
            )

        self.assertIsNone(loop._trace_sink)

    async def test_seq_stays_monotonic_across_turns_in_one_session(self) -> None:
        """红线 4：seq 是 session 内全序的唯一来源。

        每轮新建 EventBus 会让 seq 归零，同一 trace 文件里按 seq 排序就把多轮交错了。
        """
        with TemporaryDirectory() as tmpdir:
            trace_file = Path(tmpdir) / "traces" / "session-1.jsonl"
            submitted_inputs = iter(["one", "two", "three", "/exit"])
            with (
                patch.dict(os.environ, {"PICKEL_TRACE": "1"}),
                patch("pickel.cli.chat.trace_path", return_value=trace_file),
            ):
                loop = ChatLoop(
                    agent=self._build_agent(),
                    run=StubRun(),
                    session=Session.create(agent_id="Pickle", session_id="session-1"),
                    console=Mock(),
                    input_reader=lambda _: next(submitted_inputs),
                )
                await loop.run()

            seqs = [
                json.loads(line)["seq"]
                for line in trace_file.read_text(encoding="utf-8").splitlines()
            ]

        self.assertEqual([0, 1, 2], seqs)

    async def test_turn_renderer_is_unsubscribed_so_later_turns_render_once(self) -> None:
        """bus 长命后，每轮的渲染器必须退订，否则第 N 轮打印 N 遍。"""
        console = Mock()
        submitted_inputs = iter(["one", "two", "three", "/exit"])
        loop = ChatLoop(
            agent=self._build_agent(),
            run=StubRun(),
            session=Session.create(agent_id="Pickle", session_id="session-1"),
            console=console,
            input_reader=lambda _: next(submitted_inputs),
        )

        await loop.run()

        titles = [getattr(call.args[0], "title", None) for call in console.print.call_args_list]
        self.assertEqual(3, titles.count("Assistant"))

    async def test_失败轮也退订渲染器_后续轮次不翻倍(self) -> None:
        """异常路径的退订只由 finally 保证，挪出去就是「一轮失败后越印越多」。"""
        console = Mock()
        submitted_inputs = iter(["one", "two", "three", "/exit"])
        loop = ChatLoop(
            agent=self._build_agent(),
            run=ErrorRun(),
            session=Session.create(agent_id="Pickle", session_id="session-1"),
            console=console,
            input_reader=lambda _: next(submitted_inputs),
        )

        await loop.run()

        panels = [call.args[0] for call in console.print.call_args_list]
        titles = [getattr(panel, "title", None) for panel in panels]
        errors = [
            panel
            for panel in panels
            if getattr(panel, "title", None) == "System"
            and getattr(panel, "border_style", None) == "red"
        ]

        self.assertEqual(3, len(errors))
        # 每轮 1 次：渲染器没退订的话第 2/3 轮会打 2/3 遍
        self.assertEqual(3, titles.count("Thinking"))
        self.assertEqual(0, titles.count("Assistant"))

    async def test_真实_run_端到端_事件序列_seq_trace_与渲染次数(self) -> None:
        """组装层集成：真 Run + ReAct + ChatLoop + 长命 bus + trace sink。

        tests/runs/* 碰不到 trace sink 与渲染器订阅，tests/cli/* 又只用桩 Run，
        这条把两个半场接起来，钉住一次真实 turn 的完整事件序列。
        """
        from tests.runs.test_events import DelayEchoTool, StubProvider

        with TemporaryDirectory() as tmpdir:
            home = Path(tmpdir)
            agent = self._build_agent()
            run = Run(
                agent=agent,
                provider=StubProvider(
                    responses=[
                        AssistantMessage(
                            content=[
                                ToolCallContent(
                                    id="call-1",
                                    name="echo",
                                    arguments={"text": "hi"},
                                )
                            ],
                            metadata=_model_metadata(),
                        ),
                        AssistantMessage(
                            content=[TextContent(text="done")],
                            metadata=_model_metadata(),
                        ),
                    ]
                ),
                tool_bus=(_bus := bus_with([DelayEchoTool()])),
                activation=ToolActivation(allowed=frozenset(_bus.list_names())),
                context_assembler=ContextAssembler(),
                lifecycle_hooks=NoopLifecycleHooks(),
                session_service=None,
                file_access_policy=None,
                workspace_files=None,
                shell_session_manager=ShellSessionManager(),
                unit_window=5,
                strategy=ReActStrategy(max_steps=4),
            )
            console = Mock()
            submitted_inputs = iter(["hello", "/exit"])
            # 不 patch trace_path：走真实实现（PICKEL_HOME 改写 home_dir）
            with patch.dict(os.environ, {"PICKEL_TRACE": "1", "PICKEL_HOME": str(home)}):
                loop = ChatLoop(
                    agent=agent,
                    run=run,
                    session=Session.create(agent_id="Pickle", session_id="session-1"),
                    console=console,
                    input_reader=lambda _: next(submitted_inputs),
                )
                await loop.run()

            trace_file = home / "traces" / "session-1.jsonl"
            records = [
                json.loads(line)
                for line in trace_file.read_text(encoding="utf-8").splitlines()
            ]

        self.assertEqual(
            [
                "turn_started",
                "step_started",
                "tool_call_started",
                "tool_call_completed",
                "step_started",
                "assistant_message",
                "turn_completed",
            ],
            [record["event_type"] for record in records],
        )
        self.assertEqual(list(range(7)), [record["seq"] for record in records])
        self.assertEqual({"session-1"}, {record["session_id"] for record in records})
        self.assertEqual(1, len({record["turn_id"] for record in records}))
        self.assertEqual(
            {"text": "hi"}, records[2]["tool_call"]["arguments"]
        )
        self.assertEqual("hi", records[3]["tool_result"]["content"])
        self.assertEqual("done", records[5]["text"])
        # 同一 turn 的两个事件必须给出同一份 usage
        self.assertEqual(
            {"steps": 2, "input_tokens": 200, "output_tokens": 20},
            {
                key: records[5]["usage"][key]
                for key in ("steps", "input_tokens", "output_tokens")
            },
        )
        self.assertEqual(records[5]["usage"], records[6]["usage"])

        titles = [getattr(call.args[0], "title", None) for call in console.print.call_args_list]
        self.assertEqual(
            ["MyOpenClaw Chat", "Thinking", "Tool", "Tool", "Thinking", "Assistant", "System"],
            titles,
        )

    async def test_new_session_rebuilds_trace_sink_for_new_session_id(self) -> None:
        """/new 换了 session，trace 文件必须跟着换，否则两个 session 混在一个文件里。"""
        with TemporaryDirectory() as tmpdir:
            traces = Path(tmpdir)
            submitted_inputs = iter(["hello", "/new", "hello", "/exit"])
            with (
                patch.dict(os.environ, {"PICKEL_TRACE": "1"}),
                patch(
                    "pickel.cli.chat.trace_path",
                    side_effect=lambda session_id: traces / f"{session_id}.jsonl",
                ),
            ):
                loop = ChatLoop(
                    agent=self._build_agent(),
                    run=StubRun(),
                    session=Session.create(agent_id="Pickle", session_id="session-1"),
                    console=Mock(),
                    input_reader=lambda _: next(submitted_inputs),
                )
                await loop.run()
                new_session_id = loop.session.session_id

            written = sorted(path.name for path in traces.glob("*.jsonl"))
            first = json.loads(
                (traces / "session-1.jsonl").read_text(encoding="utf-8").strip()
            )
            second = json.loads(
                (traces / f"{new_session_id}.jsonl").read_text(encoding="utf-8").strip()
            )

        self.assertNotEqual("session-1", new_session_id)
        self.assertEqual(2, len(written))
        self.assertEqual("session-1", first["session_id"])
        self.assertEqual(new_session_id, second["session_id"])

    async def test_trace_open_failure_does_not_break_startup(self) -> None:
        """可观测性组件不得弄挂主流程：traces 目录不可写时降级而非崩。"""
        console = Mock()
        submitted_inputs = iter(["hello", "/exit"])
        with (
            patch.dict(os.environ, {"PICKEL_TRACE": "1"}),
            patch(
                "pickel.cli.chat.JsonlTraceSink",
                side_effect=PermissionError("Permission denied: ~/.pickel/traces"),
            ),
        ):
            loop = ChatLoop(
                agent=self._build_agent(),
                run=StubRun(),
                session=Session.create(agent_id="Pickle", session_id="session-1"),
                console=console,
                input_reader=lambda _: next(submitted_inputs),
            )
            await loop.run()

        self.assertIsNone(loop._trace_sink)
        titles = [getattr(call.args[0], "title", None) for call in console.print.call_args_list]
        self.assertEqual(1, titles.count("Assistant"))

    async def test_help_lists_context_command(self) -> None:
        output = StringIO()
        console = Console(file=output, force_terminal=False, width=120, record=True)
        submitted_inputs = iter(["/help", "/exit"])
        loop = ChatLoop(
            agent=self._build_agent(),
            run=SilentRun(),
            session=Session.create(agent_id="Pickle", session_id="session-1"),
            console=console,
            input_reader=lambda _: next(submitted_inputs),
        )

        await loop.run()

        rendered = console.export_text()
        self.assertIn("/context", rendered)

    async def test_header_lists_context_command(self) -> None:
        output = StringIO()
        console = Console(file=output, force_terminal=False, width=120, record=True)
        submitted_inputs = iter(["/exit"])
        loop = ChatLoop(
            agent=self._build_agent(),
            run=SilentRun(),
            session=Session.create(agent_id="Pickle", session_id="session-1"),
            console=console,
            input_reader=lambda _: next(submitted_inputs),
        )

        await loop.run()

        rendered = console.export_text()
        self.assertIn("/model", rendered)
        self.assertIn("/agent", rendered)
        self.assertIn("/reload", rendered)

    def _context_loop(self, *, session, console, inputs, run=None):
        agent = self._build_agent()
        return ChatLoop(
            agent=agent,
            run=run if run is not None else StubContextRun(agent),
            session=session,
            console=console,
            input_reader=lambda _: next(inputs),
            context_renderer=ContextRenderer(),
        )

    async def test_context_command_renders_token_categories_and_bar(self) -> None:
        output = StringIO()
        console = Console(file=output, force_terminal=False, width=120, record=True)
        submitted_inputs = iter(["/context", "/exit"])
        session = Session.create(agent_id="Pickle", session_id="session-1")
        run = StubContextRun(self._build_agent())
        run.provider.count_context_tokens = AsyncMock(return_value=None)

        loop = self._context_loop(
            session=session,
            console=console,
            inputs=submitted_inputs,
            run=run,
        )
        await loop.run()

        rendered = console.export_text()
        self.assertIn("Context Usage", rendered)
        self.assertIn("By category", rendered)
        self.assertIn("tokens", rendered)
        # §7.4：不再是 sections/messages/tools 的个数 dump
        self.assertNotIn("system_sections=", rendered)

    async def test_context_command_does_not_execute_recall(self) -> None:
        """§7.3 / §11.6：预览不得执行 recall（含远程 OV）。"""
        output = StringIO()
        console = Console(file=output, force_terminal=False, width=120, record=True)
        submitted_inputs = iter(["/context", "/exit"])
        run = StubContextRun(self._build_agent())
        run.provider.count_context_tokens = AsyncMock(return_value=None)
        recall = ExplodingRecall()
        run.recall_sources = [recall]

        loop = self._context_loop(
            session=Session.create(agent_id="Pickle", session_id="session-1"),
            console=console,
            inputs=submitted_inputs,
            run=run,
        )
        await loop.run()

        self.assertEqual(0, recall.calls)
        self.assertIn("recall skipped", console.export_text())

    async def test_context_command_reads_last_usage_from_session(self) -> None:
        """§11.8：last usage 只从 Session 派生，新进程读同一 Session 仍能显示。"""
        output = StringIO()
        console = Console(file=output, force_terminal=False, width=120, record=True)
        submitted_inputs = iter(["/context", "/exit"])
        session = Session.create(agent_id="Pickle", session_id="session-1")
        session.append_user(UserMessage(content=[TextContent(text="hi")]))
        session.append_assistant(
            AssistantMessage(
                content=[TextContent(text="hello")],
                metadata=ModelResponseMetadata(
                    provider="google/gemini",
                    model="gemini-3-flash-preview",
                    elapsed_ms=1234,
                    usage=ModelUsage(
                        input_tokens=111,
                        output_tokens=22,
                        cache_read_tokens=8000,
                    ),
                ),
            )
        )
        run = StubContextRun(self._build_agent())
        run.provider.count_context_tokens = AsyncMock(return_value=None)

        # 全新 ChatLoop 实例：从未在本进程内跑过 turn
        loop = self._context_loop(
            session=session,
            console=console,
            inputs=submitted_inputs,
            run=run,
        )
        await loop.run()

        rendered = console.export_text()
        self.assertIn("Last turn", rendered)
        # 实际输入 = 111 + 8000 + 0
        self.assertIn("8,111", rendered)
        self.assertIn("1,234", rendered)

    async def test_context_command_survives_prepare_failure(self) -> None:
        output = StringIO()
        console = Console(file=output, force_terminal=False, width=120, record=True)
        submitted_inputs = iter(["/context", "/exit"])
        run = StubContextRun(self._build_agent())
        run.unit_window = "not-an-int"  # 触发 prepare 内部异常

        loop = self._context_loop(
            session=Session.create(agent_id="Pickle", session_id="session-1"),
            console=console,
            inputs=submitted_inputs,
            run=run,
        )
        await loop.run()

        self.assertIn("组装失败", console.export_text())

    async def test_session_command_renders_preview(self) -> None:
        output = StringIO()
        console = Console(file=output, force_terminal=False, width=120, record=True)
        submitted_inputs = iter(["/session", "/exit"])
        session_service = FakeSessionService()
        loop = ChatLoop(
            agent=self._build_agent(),
            run=SilentRun(),
            session=Session.create(agent_id="Pickle", session_id="session-1"),
            console=console,
            input_reader=lambda _: next(submitted_inputs),
            session_service=session_service,
        )

        await loop.run()

        rendered = console.export_text()
        self.assertIn("session-1", rendered)
        self.assertIn("runtime reply", rendered)

    async def test_from_boot_uses_react_max_steps_from_app_config(self) -> None:
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "agents" / "Pickle").mkdir(parents=True)
            (root / "agents" / "Pickle" / "AGENT.md").write_text("You are Pickle.\n")
            (root / "workspace").mkdir()
            config_path = root / "config.yaml"
            config_path.write_text(
                textwrap.dedent(
                    """
                    default_agent: Pickle
                    react_max_steps: 16
                    default_llm:
                      provider: google/gemini
                      model: gemini-3-flash-preview
                    providers:
                      google/gemini:
                        models:
                          gemini-3-flash-preview:
                            temperature: 1.0
                            max_output_tokens: 1024
                            provider_options: {}
                    agents:
                      Pickle:
                        workspace_path: workspace
                        behavior_path: agents/Pickle
                    """
                ).strip()
            )

            from pickel.app.boot import Boot
            from tests.helpers.yaml_app_config import app_config_from_yaml_file

            loop = ChatLoop.from_boot(
                boot=Boot.from_config(app_config_from_yaml_file(config_path))
            )

            self.assertEqual(16, loop._run.strategy.max_steps)


if __name__ == "__main__":
    unittest.main()
