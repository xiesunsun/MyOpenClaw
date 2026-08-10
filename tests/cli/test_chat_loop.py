import asyncio
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
from pickel.context.model_context_builder import ModelContextBuilder
from pickel.conversations.agent_message import AssistantMessage, UserMessage
from pickel.conversations.content_blocks import TextContent, ToolCallContent
from pickel.hooks.lifecycle import NoopLifecycleHooks
from pickel.runs import ReActStrategy, Run
from pickel.tools.shell import LocalBashOperations
from pickel.conversations.metadata import MessageMetadata
from pickel.conversations.session import Session
from pickel.conversations.session_entry import SessionEntry
from pickel.conversations.session_preview import SessionPreview
from pickel.cli.chat import ChatLoop
from pickel.app.runtime_models import McpInspection, McpServerInfo
from pickel.conversations.agent_message import ModelResponseMetadata, ModelUsage
from pickel.runs.usage_anchor import context_fingerprint
from pickel.runs.event_bus import EventBus
from pickel.runs.runtime_events import (
    AssistantMessageEvent,
    StepStarted,
    TextDeltaEvent,
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
from tests.cli.helpers import chat_loop


def _assistant_body_prints(console: Mock, text: str) -> list:
    """事件渲染出的 assistant 白字正文次数（不再使用 Markdown）。"""
    return [
        call.args[0]
        for call in console.print.call_args_list
        if call.args and call.args[0] == text
    ]


def _assistant_text(message: AssistantMessage) -> str:
    return "\n".join(
        block.text
        for block in message.content
        if isinstance(block, TextContent) and block.text
    )


def _user_text(message: UserMessage) -> str:
    return "\n".join(
        block.text
        for block in message.content
        if isinstance(block, TextContent) and block.text
    )


def _model_metadata() -> ModelResponseMetadata:
    return ModelResponseMetadata(
        provider="google/gemini",
        model="gemini-3-flash-preview",
        elapsed_ms=100,
        usage=ModelUsage(input_tokens=100, output_tokens=10),
    )


def _text_assistant(
    text: str, *, metadata: MessageMetadata | None = None
) -> AssistantMessage:
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
        user_message: UserMessage,
        bus: EventBus | None = None,
    ) -> AssistantMessage:
        session.append_user(user_message)
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
        user_message: UserMessage,
        bus: EventBus | None = None,
    ) -> AssistantMessage:
        session.append_user(user_message)
        reply = _text_assistant("runtime reply")
        session.append_assistant(reply)
        return reply


class ErrorRun:
    async def turn(
        self,
        *,
        session: Session,
        user_message: UserMessage,
        bus: EventBus | None = None,
    ) -> AssistantMessage:
        session.append_user(user_message)
        # 真 Run 在失败前也已经发过事件；桩照做，否则「失败轮不退订渲染器」
        # 这种回归在后续轮次里没有任何可观测痕迹。StepStarted 在 E3 已不上
        # 屏，可观测痕迹靠流式 delta。
        if bus is not None:
            await bus.emit(StepStarted(envelope=EventEnvelope(step_index=1)))
            await bus.emit(TextDeltaEvent(text="failing"))
        raise ValueError("boom")


class RecordingRun:
    """记录 turn 的开始与完成；被 cancel 掉的 turn 两个列表里都不会出现。"""

    def __init__(self) -> None:
        self.started: list[str] = []
        self.completed: list[str] = []

    async def turn(
        self,
        *,
        session: Session,
        user_message: UserMessage,
        bus: EventBus | None = None,
    ) -> AssistantMessage:
        self.started.append(_user_text(user_message))
        session.append_user(user_message)
        reply = _text_assistant("runtime reply")
        session.append_assistant(reply)
        self.completed.append(_user_text(user_message))
        return reply


class _InterruptedTurnTask:
    """真 task 的透明包装：首次 await 在 task 尚未运行前同步抛 KeyboardInterrupt。

    KeyboardInterrupt 若从 task 协程内部抛出，会经 Task.__step 直接砸进
    事件循环把测试进程杀掉——真实 Ctrl-C 抵达的是 `_loop` 的 `await task`
    点，所以只能在 await 侧注入。cancel 与后续 await 全部转发给真 task，
    因此 cancel 有真实可观察效果：漏掉 task.cancel()，被中断的 turn 会跑完。
    """

    def __init__(self, coro) -> None:
        self.task = asyncio.get_running_loop().create_task(coro)
        self.cancel_calls = 0
        self._interrupted = False

    def cancel(self) -> bool:
        self.cancel_calls += 1
        return self.task.cancel()

    def __await__(self):
        if not self._interrupted:
            self._interrupted = True
            raise KeyboardInterrupt
        return self.task.__await__()


def _interrupt_first_turn_at_await(created: list["_InterruptedTurnTask"]):
    """create_task 替身：第一条 turn 换成注入 KI 的包装，其余照常建真 task。"""

    def create_task(coro):
        if created:
            return asyncio.get_running_loop().create_task(coro)
        wrapper = _InterruptedTurnTask(coro)
        created.append(wrapper)
        return wrapper

    return create_task


class StubToolRun:
    async def turn(
        self,
        *,
        session: Session,
        user_message: UserMessage,
        bus: EventBus | None = None,
    ) -> AssistantMessage:
        session.append_user(user_message)
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
        self.model_context_builder = ModelContextBuilder()
        self.strategy = Mock()

    async def turn(
        self,
        *,
        session: Session,
        user_message: UserMessage,
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

    def flush_new_entries(
        self, *, session: Session, entries: list[SessionEntry]
    ) -> None:
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

    async def test_handle_user_input_delegates_to_coordinator_and_updates_session_count(
        self,
    ) -> None:
        agent = self._build_agent()
        session = Session.create(agent_id="Pickle", session_id="session-1")
        loop = chat_loop(
            agent=agent,
            run=StubRun(),
            session=session,
        )

        result = await loop.handle_user_input("hello")

        self.assertEqual("runtime reply", _assistant_text(result.message))
        self.assertEqual(2, loop._message_count())

    async def test_chat_loop_creates_session_from_conversation_layer(self) -> None:
        agent = self._build_agent()

        loop = chat_loop(
            agent=agent,
            run=StubRun(),
        )

        self.assertEqual("Pickle", loop.session.agent_id)

    async def test_handle_user_input_renders_tool_batch_progress_before_final_reply(
        self,
    ) -> None:
        agent = self._build_agent()
        session = Session.create(agent_id="Pickle", session_id="session-1")
        console = Console(file=StringIO(), force_terminal=False, width=120, record=True)
        loop = chat_loop(
            agent=agent,
            run=StubToolRun(),
            session=session,
            console=console,
        )

        bus, _, _unsubscribe = loop.create_event_bus()
        result = await loop.handle_user_input("hello")

        rendered = console.export_text()

        self.assertEqual("final reply", _assistant_text(result.message))
        self.assertIn("⏺ read_file", rendered)
        self.assertIn("path=", rendered)
        self.assertIn("running", rendered)
        self.assertIn("ok", rendered)
        self.assertIn("out", rendered)
        self.assertIn("file content", rendered)
        self.assertIn("final reply", rendered)
        self.assertIn("google/gemini / gemini-3-flash-preview · 11→7", rendered)
        # 工具行先于最终正文
        self.assertLess(rendered.index("⏺ read_file"), rendered.index("final reply"))
        self.assertNotIn("Step 1", rendered)  # E3：Step 行不再上屏
        self.assertNotIn("shell_status", rendered)  # metadata 不上屏
        self.assertNotIn("╭", rendered)  # 无 Panel

    @patch("pickel.cli.chat.PromptToolkitInputReader")
    async def test_chat_loop_uses_prompt_toolkit_reader_by_default(
        self, prompt_reader_cls: Mock
    ) -> None:
        prompt_reader = AsyncMock(return_value="hello")
        prompt_reader_cls.return_value = prompt_reader

        loop = chat_loop(
            agent=self._build_agent(),
            run=StubRun(),
        )

        self.assertEqual("hello", await loop.input_reader("You > "))
        prompt_reader_cls.assert_called_once_with()
        prompt_reader.assert_called_once_with("You > ")

    async def test_无事件的轮次不再有_fallback_渲染(self) -> None:
        """E3：渲染唯一入口是事件订阅。不发事件的 Run 意味着 runtime 违约，
        chat.py 不得替它把正文再画一遍（旧 fallback 已删）。"""
        output = StringIO()
        console = Console(file=output, force_terminal=False, width=120, record=True)
        submitted_inputs = iter(["hello", "/exit"])
        loop = chat_loop(
            agent=self._build_agent(),
            run=SilentRun(),
            session=Session.create(agent_id="Pickle", session_id="session-1"),
            console=console,
            input_reader=lambda _: next(submitted_inputs),
        )

        await loop.run()

        rendered = console.export_text()
        self.assertNotIn("runtime reply", rendered)
        self.assertIn("Session closed.", rendered)

    async def test_footer_无用量时退到当前模型_label(self) -> None:
        """E2 遗留：usage=None 时 footer 不再整体消失，显示当前模型。
        label 从 ChatLoop.agent.model_config 注入（/model 切换后跟随更新）。"""
        output = StringIO()
        console = Console(file=output, force_terminal=False, width=120, record=True)
        submitted_inputs = iter(["hello", "/exit"])
        loop = chat_loop(
            agent=self._build_agent(),
            run=StubRun(),  # 发 AssistantMessageEvent 且 usage=None
            session=Session.create(agent_id="Pickle", session_id="session-1"),
            console=console,
            input_reader=lambda _: next(submitted_inputs),
        )

        await loop.run()

        rendered = console.export_text()
        self.assertIn("runtime reply", rendered)
        self.assertIn("google/gemini / gemini-3-flash-preview", rendered)

    async def test_真实_run_流式与工具_无边框渲染顺序与_footer(self) -> None:
        """E3 组装层集成：真 Run + 流式 provider + 工具，一轮完整无边框输出。

        钉住：无 Panel 残留、⏺ 工具行与结果行、思考中前缀、footer 三段式
        （§5.1 口径的 in→out）、各段落顺序。
        """
        from tests.runs.test_events import DelayEchoTool

        class _StreamingToolProvider:
            """两步：先 tool_call（无增量），再 thinking+text 增量。"""

            def __init__(self) -> None:
                self._step = 0

            async def stream(self, context):
                from pickel.providers.stream import (
                    StreamCompleted,
                    TextDelta,
                    ThinkingDelta,
                )

                self._step += 1
                if self._step == 1:
                    yield StreamCompleted(
                        message=AssistantMessage(
                            content=[
                                ToolCallContent(
                                    id="call-1", name="echo", arguments={"text": "hi"}
                                )
                            ],
                            metadata=_model_metadata(),
                        )
                    )
                    return
                yield ThinkingDelta(text="想一下")
                yield TextDelta(text="do")
                yield TextDelta(text="ne")
                yield StreamCompleted(
                    message=AssistantMessage(
                        content=[TextContent(text="done")],
                        metadata=_model_metadata(),
                    )
                )

            async def generate(self, context):
                from pickel.providers.stream import accumulate

                return await accumulate(self.stream(context))

        agent = self._build_agent()
        run = Run(
            agent=agent,
            provider=_StreamingToolProvider(),
            tool_bus=(_bus := bus_with([DelayEchoTool()])),
            activation=ToolActivation(allowed=frozenset(_bus.list_names())),
            model_context_builder=ModelContextBuilder(),
            lifecycle_hooks=NoopLifecycleHooks(),
            session_service=None,
            file_access_policy=None,
            workspace_files=None,
            bash_operations=LocalBashOperations(),
            unit_window=5,
            strategy=ReActStrategy(max_steps=4),
        )
        console = Console(file=StringIO(), force_terminal=False, width=120, record=True)
        submitted_inputs = iter(["hello", "/exit"])
        loop = chat_loop(
            agent=agent,
            run=run,
            session=Session.create(agent_id="Pickle", session_id="session-1"),
            console=console,
            input_reader=lambda _: next(submitted_inputs),
        )

        await loop.run()

        rendered = console.export_text()
        self.assertNotIn("╭", rendered)
        self.assertIn("⏺ echo", rendered)
        self.assertIn("ok", rendered)
        self.assertIn("out", rendered)
        self.assertIn("hi", rendered)
        self.assertIn("· 思考中……", rendered)
        self.assertIn("done", rendered)
        # 两步合计：input 100+100=200、output 10+10=20、elapsed 100+100ms=0.2s
        self.assertIn(
            "google/gemini / gemini-3-flash-preview · 200→20" " · cache r0/w0 · 0.2s",
            rendered,
        )
        # 顺序：工具行 < ok < 思考行 < footer；正文 settle 后一份
        self.assertLess(rendered.index("⏺ echo"), rendered.index("ok"))
        self.assertLess(rendered.index("ok"), rendered.index("· 思考中……"))
        self.assertLess(
            rendered.index("· 思考中……"),
            rendered.index("google/gemini / gemini-3-flash-preview · 200→20"),
        )
        self.assertEqual(
            1,
            sum(1 for line in rendered.splitlines() if line.strip() == "done"),
            "settle 后最终正文只一份",
        )

    async def test_run_does_not_duplicate_final_reply_after_assistant_event(
        self,
    ) -> None:
        console = Mock()
        submitted_inputs = iter(["hello", "/exit"])
        loop = chat_loop(
            agent=self._build_agent(),
            run=StubRun(),
            session=Session.create(agent_id="Pickle", session_id="session-1"),
            console=console,
            input_reader=lambda _: next(submitted_inputs),
        )

        await loop.run()

        titles = [
            getattr(call.args[0], "title", None)
            for call in console.print.call_args_list
        ]
        self.assertEqual(1, len(_assistant_body_prints(console, "runtime reply")))
        # chat.py 的 fallback（Panel "Assistant"）不得再画一遍
        self.assertEqual(0, titles.count("Assistant"))
        self.assertNotIn("You", titles)

    async def test_run_renders_stable_runtime_error_when_turn_fails(self) -> None:
        output = StringIO()
        console = Console(file=output, force_terminal=False, width=120, record=True)
        submitted_inputs = iter(["hello", "/exit"])
        loop = chat_loop(
            agent=self._build_agent(),
            run=ErrorRun(),
            session=Session.create(agent_id="Pickle", session_id="session-1"),
            console=console,
            input_reader=lambda _: next(submitted_inputs),
        )

        await loop.run()

        rendered = console.export_text()
        self.assertNotIn("Traceback (most recent call last):", rendered)
        self.assertIn("ValueError: boom", rendered)

    async def test_run_flushes_new_messages_after_turn(self) -> None:
        console = Mock()
        submitted_inputs = iter(["hello", "/exit"])
        session_service = FakeSessionService()
        loop = chat_loop(
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

    async def test_run_uses_existing_message_count_as_local_flush_start_index(
        self,
    ) -> None:
        console = Mock()
        submitted_inputs = iter(["hello", "/exit"])
        session_service = FakeSessionService()
        session = Session.create(agent_id="Pickle", session_id="session-1")
        session.append_user(UserMessage(content=[TextContent(text="previous")]))
        session.append_assistant(_text_assistant("old reply"))
        loop = chat_loop(
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
        loop = chat_loop(
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
                loop = chat_loop(
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
                if json.loads(line).get("record_type") == "runtime_event"
            ]

        self.assertEqual(["assistant_message"], [e["event_type"] for e in events])
        self.assertIsNone(loop._trace_sink)
        self.assertTrue(sink.closed)

    async def test_keyboard_interrupt_cancels_turn_and_loop_continues(self) -> None:
        """Ctrl-C 只中断当前 turn：cancel 本轮 task，循环继续吃后续输入。

        注入方式见 _InterruptedTurnTask。三个语义各有断言：
        中断轮被取消（cancel 被调、turn 未跑）、循环继续（下一条输入
        照常跑完，`continue` 改 `return` 会让它消失）、sink 最终释放。
        """
        run = RecordingRun()
        created: list[_InterruptedTurnTask] = []

        with TemporaryDirectory() as tmpdir:
            trace_file = Path(tmpdir) / "traces" / "session-1.jsonl"
            submitted_inputs = iter(["first", "second", "/exit"])
            with (
                patch.dict(os.environ, {"PICKEL_TRACE": "1"}),
                patch("pickel.cli.chat.trace_path", return_value=trace_file),
                patch(
                    "pickel.cli.chat.asyncio.create_task",
                    side_effect=_interrupt_first_turn_at_await(created),
                ),
            ):
                loop = chat_loop(
                    agent=self._build_agent(),
                    run=run,
                    session=Session.create(agent_id="Pickle", session_id="session-1"),
                    console=Mock(),
                    input_reader=lambda _: next(submitted_inputs),
                )
                sink = loop._trace_sink
                # 新语义：turn 内 Ctrl-C 不再冒出 run()，而是取消本轮后回到输入
                await loop.run()

        # 中断轮被取消：cancel 被调，turn 从未跑起来
        self.assertEqual(1, created[0].cancel_calls)
        self.assertTrue(created[0].task.cancelled())
        # 循环继续：第二条输入照常完成
        self.assertEqual(["second"], run.started)
        self.assertEqual(["second"], run.completed)
        self.assertTrue(sink.closed)

    async def test_keyboard_interrupt_still_flushes_session_to_disk(self) -> None:
        """中断分支必须 flush：react 取消时补齐的 tool_result 要落盘，
        否则下一轮从磁盘恢复的 session 仍有悬空 tool_call。"""
        session_service = FakeSessionService()
        created: list[_InterruptedTurnTask] = []

        submitted_inputs = iter(["first", "/exit"])
        with patch(
            "pickel.cli.chat.asyncio.create_task",
            side_effect=_interrupt_first_turn_at_await(created),
        ):
            loop = chat_loop(
                agent=self._build_agent(),
                run=RecordingRun(),
                session=Session.create(agent_id="Pickle", session_id="session-1"),
                console=Mock(),
                input_reader=lambda _: next(submitted_inputs),
                session_service=session_service,
            )
            await loop.run()

        # 全程只有中断这一轮，唯一一次 flush 只能来自中断分支
        self.assertEqual([[]], session_service.flush_calls)
        self.assertTrue(session_service.closed)

    async def test_trace_standard_by_default_builds_sink(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PICKEL_TRACE", None)
            loop = chat_loop(
                agent=self._build_agent(),
                run=StubRun(),
                session=Session.create(agent_id="Pickle", session_id="session-1"),
                console=Mock(),
            )

        self.assertIsNotNone(loop._trace_sink)
        loop._close_trace_sink()

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
                loop = chat_loop(
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
                if "seq" in json.loads(line)
            ]

        self.assertEqual([0, 1, 2], seqs)

    async def test_turn_renderer_is_unsubscribed_so_later_turns_render_once(
        self,
    ) -> None:
        """bus 长命后，每轮的渲染器必须退订，否则第 N 轮打印 N 遍。"""
        console = Mock()
        submitted_inputs = iter(["one", "two", "three", "/exit"])
        loop = chat_loop(
            agent=self._build_agent(),
            run=StubRun(),
            session=Session.create(agent_id="Pickle", session_id="session-1"),
            console=console,
            input_reader=lambda _: next(submitted_inputs),
        )

        await loop.run()

        self.assertEqual(3, len(_assistant_body_prints(console, "runtime reply")))

    async def test_失败轮也退订渲染器_后续轮次不翻倍(self) -> None:
        """异常路径的退订只由 finally 保证，挪出去就是「一轮失败后越印越多」。"""
        console = Mock()
        submitted_inputs = iter(["one", "two", "three", "/exit"])
        loop = chat_loop(
            agent=self._build_agent(),
            run=ErrorRun(),
            session=Session.create(agent_id="Pickle", session_id="session-1"),
            console=console,
            input_reader=lambda _: next(submitted_inputs),
        )

        await loop.run()

        panels = [call.args[0] for call in console.print.call_args_list]
        titles = [getattr(panel, "title", None) for panel in panels]
        # E3 无框排版：error 是 "✗ " 前缀的 Text，不再是红边 Panel
        errors = [
            panel
            for panel in panels
            if isinstance(panel, Text) and str(panel).startswith("✗ ")
        ]
        deltas = [
            call
            for call in console.print.call_args_list
            if call.args and call.args[0] == "failing"
        ]

        self.assertEqual(3, len(errors))
        # 每轮 1 次：渲染器没退订的话第 2/3 轮的流式 delta 会打 2/3 遍
        self.assertEqual(3, len(deltas))
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
                model_context_builder=ModelContextBuilder(),
                lifecycle_hooks=NoopLifecycleHooks(),
                session_service=None,
                file_access_policy=None,
                workspace_files=None,
                bash_operations=LocalBashOperations(),
                unit_window=5,
                strategy=ReActStrategy(max_steps=4),
            )
            console = Console(
                file=StringIO(), force_terminal=False, width=120, record=True
            )
            submitted_inputs = iter(["hello", "/exit"])
            # 不 patch trace_path：走真实实现（PICKEL_HOME 改写 home_dir）
            with patch.dict(
                os.environ, {"PICKEL_TRACE": "1", "PICKEL_HOME": str(home)}
            ):
                loop = chat_loop(
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
                if json.loads(line).get("record_type") == "runtime_event"
            ]

        self.assertEqual(
            [
                "turn_started",
                "step_started",
                "request_digest",
                "tool_call_started",
                "tool_call_completed",
                "step_started",
                "request_digest",
                "assistant_message",
                "turn_completed",
            ],
            [record["event_type"] for record in records],
        )
        self.assertEqual(list(range(9)), [record["seq"] for record in records])
        self.assertEqual({"session-1"}, {record["session_id"] for record in records})
        self.assertEqual(1, len({record["turn_id"] for record in records}))
        self.assertEqual({"text": "hi"}, records[3]["tool_call"]["arguments"])
        self.assertEqual("hi", records[4]["tool_result"]["content"])
        self.assertEqual("done", records[7]["text"])
        # 同一 turn 的两个事件必须给出同一份 usage
        self.assertEqual(
            {"steps": 2, "input_tokens": 200, "output_tokens": 20},
            {
                key: records[7]["usage"][key]
                for key in ("steps", "input_tokens", "output_tokens")
            },
        )
        self.assertEqual(records[7]["usage"], records[8]["usage"])

        rendered = console.export_text()
        self.assertIn("⏺ echo", rendered)
        self.assertIn("ok", rendered)
        self.assertIn("out", rendered)
        self.assertIn("hi", rendered)
        self.assertIn("done", rendered)
        self.assertIn(
            "google/gemini / gemini-3-flash-preview · 200→20" " · cache r0/w0 · 0.2s",
            rendered,
        )
        # 工具行先于最终正文；Step 行不再上屏
        self.assertLess(rendered.index("⏺ echo"), rendered.rindex("done"))
        self.assertNotIn("Step 1", rendered)

    async def test_真实_run_流式_provider_trace_中_delta_先于_assistant_message_且_seq_连续(
        self,
    ) -> None:
        """组装层集成（流式）：真 Run + 流式 fake provider + ChatLoop + trace sink。

        tests/runs/test_react_streaming.py 只看 bus 上的事件对象，这条钉住
        delta 事件穿过 ChatLoop 长命 bus 落进 trace 文件后的顺序与 seq。
        """
        from typing import AsyncIterator

        from pickel.providers.stream import (
            StreamCompleted,
            StreamDelta,
            TextDelta,
            ThinkingDelta,
            accumulate,
        )

        final_reply = AssistantMessage(
            content=[TextContent(text="你好，世界")],
            metadata=_model_metadata(),
        )

        class _StreamingProvider:
            """产多个 delta 后以 StreamCompleted 收尾；generate 由 stream 实现。"""

            async def stream(self, context) -> AsyncIterator[StreamDelta]:
                yield ThinkingDelta(text="想一想")
                yield TextDelta(text="你好")
                yield TextDelta(text="，世")
                yield TextDelta(text="界")
                yield StreamCompleted(message=final_reply)

            async def generate(self, context) -> AssistantMessage:
                return await accumulate(self.stream(context))

        with TemporaryDirectory() as tmpdir:
            home = Path(tmpdir)
            agent = self._build_agent()
            run = Run(
                agent=agent,
                provider=_StreamingProvider(),
                tool_bus=(_bus := bus_with([])),
                activation=ToolActivation(allowed=frozenset(_bus.list_names())),
                model_context_builder=ModelContextBuilder(),
                lifecycle_hooks=NoopLifecycleHooks(),
                session_service=None,
                file_access_policy=None,
                workspace_files=None,
                bash_operations=LocalBashOperations(),
                unit_window=5,
                strategy=ReActStrategy(max_steps=4),
            )
            submitted_inputs = iter(["hello", "/exit"])
            with patch.dict(
                os.environ, {"PICKEL_TRACE": "full", "PICKEL_HOME": str(home)}
            ):
                loop = chat_loop(
                    agent=agent,
                    run=run,
                    session=Session.create(agent_id="Pickle", session_id="session-1"),
                    console=Mock(),
                    input_reader=lambda _: next(submitted_inputs),
                )
                await loop.run()

            trace_file = home / "traces" / "session-1.jsonl"
            lines = trace_file.read_text(encoding="utf-8").splitlines()
            # 逐行 json.loads 可读
            records = [
                json.loads(line)
                for line in lines
                if json.loads(line).get("record_type") == "runtime_event"
            ]

        # delta 事件全部出现在 assistant_message 之前，且顺序与 provider 产出一致
        kinds = [record["event_type"] for record in records]
        self.assertEqual(
            [
                "turn_started",
                "step_started",
                "request_digest",
                "thinking_delta",
                "text_delta",
                "text_delta",
                "text_delta",
                "assistant_message",
                "turn_completed",
            ],
            kinds,
        )
        last_delta = max(i for i, kind in enumerate(kinds) if kind.endswith("_delta"))
        self.assertLess(last_delta, kinds.index("assistant_message"))
        # 全事件 seq 连续（0..n-1）；seq 由 bus 按事件分配，
        # 连续即证明 trace 行数与事件数一致——没有事件被 sink 吞掉
        self.assertEqual(
            list(range(len(records))), [record["seq"] for record in records]
        )
        spans = [
            json.loads(line)
            for line in lines
            if json.loads(line).get("record_type") == "span"
        ]
        self.assertIn("pickel.provider.request", [s["payload"]["name"] for s in spans])
        self.assertIn("pickel.turn", [s["payload"]["name"] for s in spans])
        # delta 拼起来就是最终消息正文
        self.assertEqual(
            "你好，世界",
            "".join(r["text"] for r in records if r["event_type"] == "text_delta"),
        )
        self.assertEqual("你好，世界", records[7]["text"])

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
                loop = chat_loop(
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
                (traces / "session-1.jsonl").read_text(encoding="utf-8").splitlines()[0]
            )
            second = json.loads(
                (traces / f"{new_session_id}.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()[0]
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
                "tests.cli.helpers.JsonlTraceSink",
                side_effect=PermissionError("Permission denied: ~/.pickel/traces"),
            ),
        ):
            loop = chat_loop(
                agent=self._build_agent(),
                run=StubRun(),
                session=Session.create(agent_id="Pickle", session_id="session-1"),
                console=console,
                input_reader=lambda _: next(submitted_inputs),
            )
            await loop.run()

        self.assertIsNone(loop._trace_sink)
        self.assertEqual(1, len(_assistant_body_prints(console, "runtime reply")))

    async def test_help_lists_context_command(self) -> None:
        output = StringIO()
        console = Console(file=output, force_terminal=False, width=120, record=True)
        submitted_inputs = iter(["/help", "/exit"])
        loop = chat_loop(
            agent=self._build_agent(),
            run=SilentRun(),
            session=Session.create(agent_id="Pickle", session_id="session-1"),
            console=console,
            input_reader=lambda _: next(submitted_inputs),
        )

        await loop.run()

        rendered = console.export_text()
        self.assertIn("/context", rendered)
        self.assertIn("/mcp", rendered)

    def test_mcp_command_renders_discovered_and_active_tool_counts(self) -> None:
        console = Console(file=StringIO(), force_terminal=False, width=120, record=True)
        loop = chat_loop(agent=self._build_agent(), run=SilentRun(), console=console)
        loop._host = Mock()
        loop._host.inspect_mcp.return_value = McpInspection(
            available=True,
            servers=(
                McpServerInfo(
                    name="github",
                    status="connected",
                    transport="stdio",
                    config_scope="project",
                    protocol_version="2026-01-01",
                    implementation="github-mcp 1.2.0",
                    discovered_tools=12,
                    active_tools=3,
                    last_error=None,
                ),
            ),
        )

        loop._render_mcp(None)

        rendered = console.export_text()
        self.assertIn("MCP servers", rendered)
        self.assertIn("github", rendered)
        self.assertIn("connected", rendered)
        self.assertIn("12 / 3", rendered)
        self.assertIn("discovered / active", rendered)

    def test_mcp_server_detail_renders_error_and_diagnostics(self) -> None:
        console = Console(file=StringIO(), force_terminal=False, width=120, record=True)
        loop = chat_loop(agent=self._build_agent(), run=SilentRun(), console=console)
        loop._host = Mock()
        loop._host.inspect_mcp.return_value = McpInspection(
            available=True,
            servers=(
                McpServerInfo(
                    name="broken",
                    status="failed",
                    transport="stdio",
                    config_scope="global",
                    protocol_version=None,
                    implementation=None,
                    discovered_tools=0,
                    active_tools=0,
                    last_error="failed to initialize",
                ),
            ),
            diagnostics=("Environment variable TOKEN is not set",),
        )

        loop._render_mcp("broken")

        rendered = console.export_text()
        self.assertIn("MCP server: broken", rendered)
        self.assertIn("failed to initialize", rendered)
        self.assertIn("Diagnostics", rendered)
        self.assertIn("TOKEN is not set", rendered)

    def test_mcp_command_distinguishes_unavailable_and_empty_configuration(
        self,
    ) -> None:
        console = Console(file=StringIO(), force_terminal=False, width=120, record=True)
        loop = chat_loop(agent=self._build_agent(), run=SilentRun(), console=console)
        loop._host = Mock()
        loop._host.inspect_mcp.side_effect = (
            McpInspection(available=False),
            McpInspection(available=True),
            McpInspection(available=True, diagnostics=("Invalid MCP config",)),
        )

        loop._render_mcp(None)
        loop._render_mcp(None)
        loop._render_mcp(None)

        rendered = console.export_text()
        self.assertIn("disabled or unavailable", rendered)
        self.assertIn("No MCP servers configured", rendered)
        self.assertIn("No MCP servers available", rendered)

    async def test_header_lists_context_command(self) -> None:
        output = StringIO()
        console = Console(file=output, force_terminal=False, width=120, record=True)
        submitted_inputs = iter(["/exit"])
        loop = chat_loop(
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
        return chat_loop(
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
        self.assertIn("Context", rendered)
        self.assertIn("System prompt", rendered)
        self.assertIn("tokens", rendered)
        self.assertIn("◆", rendered)
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
        # 实际输入 = 111 + 8000 + 0 → abbrev 8.1k；耗时 1.2s
        self.assertIn("8.1k", rendered)
        self.assertIn("1.2s", rendered)

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
        loop = chat_loop(
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

    async def test_from_host_uses_react_max_steps_from_app_config(self) -> None:
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "agents" / "Pickle").mkdir(parents=True)
            (root / "agents" / "Pickle" / "AGENT.md").write_text("You are Pickle.\n")
            (root / "workspace").mkdir()
            config_path = root / "config.yaml"
            config_path.write_text(textwrap.dedent("""
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
                    """).strip())

            from pickel.app.boot import Boot
            from pickel.app.runtime import RuntimeHost
            from tests.helpers.yaml_app_config import app_config_from_yaml_file

            loop = ChatLoop.from_host(
                host=RuntimeHost(
                    Boot.from_config(app_config_from_yaml_file(config_path))
                )
            )

            self.assertEqual(16, loop._run.strategy.max_steps)


if __name__ == "__main__":
    unittest.main()
