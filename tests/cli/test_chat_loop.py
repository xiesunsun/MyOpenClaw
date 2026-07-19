import unittest
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
import textwrap
from unittest.mock import AsyncMock, Mock, patch

from myopenclaw.agents.agent import Agent
from myopenclaw.cli.context_renderer import ContextRenderer
from myopenclaw.conversations.agent_message import AssistantMessage, UserMessage
from myopenclaw.conversations.content_blocks import TextContent
from myopenclaw.conversations.metadata import MessageMetadata
from myopenclaw.conversations.session import Session
from myopenclaw.conversations.session_entry import SessionEntry
from myopenclaw.conversations.session_preview import SessionPreview
from myopenclaw.cli.chat import ChatLoop
from myopenclaw.runs.context_usage import (
    ContextUsageCategory,
    ContextUsageDetail,
    ContextUsageSnapshot,
)
from myopenclaw.runs import RuntimeEvent, RuntimeEventType
from myopenclaw.conversations.message import ToolCall
from myopenclaw.shared.model_config import ModelConfig
from myopenclaw.tools.base import ToolExecutionResult
from rich.console import Console
from rich.text import Text


def _assistant_text(message: AssistantMessage) -> str:
    return "\n".join(
        block.text for block in message.content if isinstance(block, TextContent) and block.text
    )


def _text_assistant(text: str, *, metadata: MessageMetadata | None = None) -> AssistantMessage:
    model_meta = None
    if metadata is not None:
        from myopenclaw.conversations.agent_message import ModelResponseMetadata

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


class StubCoordinator:
    async def run_turn(
        self,
        *,
        agent: Agent,
        session: Session,
        user_text: str,
        event_handler=None,
    ) -> AssistantMessage:
        session.append_user(UserMessage(content=[TextContent(text=user_text)]))
        reply = _text_assistant("runtime reply")
        session.append_assistant(reply)
        if event_handler is not None:
            await event_handler(
                RuntimeEvent(
                    event_type=RuntimeEventType.ASSISTANT_MESSAGE,
                    text="runtime reply",
                )
            )
        return reply


class SilentCoordinator:
    async def run_turn(
        self,
        *,
        agent: Agent,
        session: Session,
        user_text: str,
        event_handler=None,
    ) -> AssistantMessage:
        session.append_user(UserMessage(content=[TextContent(text=user_text)]))
        reply = _text_assistant("runtime reply")
        session.append_assistant(reply)
        return reply


class ErrorCoordinator:
    async def run_turn(
        self,
        *,
        agent: Agent,
        session: Session,
        user_text: str,
        event_handler=None,
    ) -> AssistantMessage:
        session.append_user(UserMessage(content=[TextContent(text=user_text)]))
        raise ValueError("boom")


class StubToolCoordinator:
    async def run_turn(
        self,
        *,
        agent: Agent,
        session: Session,
        user_text: str,
        event_handler=None,
    ) -> AssistantMessage:
        session.append_user(UserMessage(content=[TextContent(text=user_text)]))
        if event_handler is not None:
            await event_handler(
                RuntimeEvent(
                    event_type=RuntimeEventType.MODEL_STEP_STARTED,
                    step_index=1,
                )
            )
        tool_call = ToolCall(
            id="call-1",
            name="read_file",
            arguments={"path": "/tmp/" + "very-long-segment/" * 12 + "file.txt"},
        )
        if event_handler is not None:
            await event_handler(
                RuntimeEvent(
                    event_type=RuntimeEventType.TOOL_CALL_STARTED,
                    step_index=1,
                    batch_id="batch-1",
                    call_index=0,
                    total_calls=1,
                    tool_call=tool_call,
                )
            )
            await event_handler(
                RuntimeEvent(
                    event_type=RuntimeEventType.TOOL_CALL_COMPLETED,
                    step_index=1,
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
            await event_handler(
                RuntimeEvent(
                    event_type=RuntimeEventType.ASSISTANT_MESSAGE,
                    text="final reply",
                    metadata=MessageMetadata(
                        provider="google/gemini",
                        model="gemini-3-flash-preview",
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


class StubContextCoordinator:
    def __init__(self, agent: Agent) -> None:
        self.deps = Mock(
            agent=agent,
            provider=Mock(),
            tools=[],
            unit_window=5,
            context_assembler=Mock(),
        )

    async def run_turn(
        self,
        *,
        agent: Agent,
        session: Session,
        user_text: str,
        event_handler=None,
    ) -> AssistantMessage:
        raise AssertionError("run_turn should not be called")


class StubContextUsageService:
    def __init__(self, snapshot: ContextUsageSnapshot) -> None:
        self.snapshot = snapshot
        self.calls: list[tuple[Agent, object, list[object] | None]] = []

    async def build(
        self,
        *,
        agent: Agent,
        context: object,
        prompt_messages=None,
    ) -> ContextUsageSnapshot:
        self.calls.append((agent, context, prompt_messages))
        return self.snapshot


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
            coordinator=StubCoordinator(),
            session=session,
        )

        result = await loop.handle_user_input("hello")

        self.assertEqual("runtime reply", _assistant_text(result))
        self.assertEqual(2, loop._message_count())

    async def test_chat_loop_creates_session_from_conversation_layer(self) -> None:
        agent = self._build_agent()

        loop = ChatLoop(
            agent=agent,
            coordinator=StubCoordinator(),
        )

        self.assertEqual("Pickle", loop.session.agent_id)

    async def test_handle_user_input_renders_tool_batch_progress_before_final_reply(self) -> None:
        agent = self._build_agent()
        session = Session.create(agent_id="Pickle", session_id="session-1")
        console = Mock()
        loop = ChatLoop(
            agent=agent,
            coordinator=StubToolCoordinator(),
            session=session,
            console=console,
        )

        result = await loop.handle_user_input(
            "hello",
            event_handler=loop.create_event_handler(),
        )

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
        from myopenclaw.conversations.agent_message import ToolResultMessage

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
            coordinator=StubCoordinator(),
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

    @patch("myopenclaw.cli.chat.PromptToolkitInputReader")
    async def test_chat_loop_uses_prompt_toolkit_reader_by_default(self, prompt_reader_cls: Mock) -> None:
        prompt_reader = AsyncMock(return_value="hello")
        prompt_reader_cls.return_value = prompt_reader

        loop = ChatLoop(
            agent=self._build_agent(),
            coordinator=StubCoordinator(),
        )

        self.assertEqual("hello", await loop.input_reader("You > "))
        prompt_reader_cls.assert_called_once_with()
        prompt_reader.assert_called_once_with("You > ")

    async def test_run_falls_back_to_render_final_reply_when_no_event_was_emitted(self) -> None:
        console = Mock()
        submitted_inputs = iter(["hello", "/exit"])
        loop = ChatLoop(
            agent=self._build_agent(),
            coordinator=SilentCoordinator(),
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
            coordinator=StubCoordinator(),
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
            coordinator=ErrorCoordinator(),
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
            coordinator=SilentCoordinator(),
            session=Session.create(agent_id="Pickle", session_id="session-1"),
            console=console,
            input_reader=lambda _: next(submitted_inputs),
            session_service=session_service,
        )

        await loop.run()

        # ChatLoop post-turn flush is empty (coordinator already checkpoint-flushed via deps).
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
            coordinator=SilentCoordinator(),
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
            coordinator=SilentCoordinator(),
            session=Session.create(agent_id="Pickle", session_id="session-1"),
            console=console,
            input_reader=lambda _: next(submitted_inputs),
            session_service=session_service,
        )

        await loop.run()

        self.assertTrue(session_service.closed)
        self.assertEqual("session-1", session_service.closed_sessions[0].session_id)

    async def test_help_lists_context_command(self) -> None:
        output = StringIO()
        console = Console(file=output, force_terminal=False, width=120, record=True)
        submitted_inputs = iter(["/help", "/exit"])
        loop = ChatLoop(
            agent=self._build_agent(),
            coordinator=SilentCoordinator(),
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
            coordinator=SilentCoordinator(),
            session=Session.create(agent_id="Pickle", session_id="session-1"),
            console=console,
            input_reader=lambda _: next(submitted_inputs),
        )

        await loop.run()

        rendered = console.export_text()
        self.assertIn("/help  /context  /clear  /session  /exit", rendered)

    async def test_context_command_renders_usage_summary(self) -> None:
        output = StringIO()
        console = Console(file=output, force_terminal=False, width=120, record=True)
        submitted_inputs = iter(["/context", "/exit"])
        snapshot = ContextUsageSnapshot(
            model_label="google/gemini / gemini-3-flash-preview",
            max_input_tokens=1048576,
            total_tokens=7000,
            categories=[
                ContextUsageCategory(key="system", label="System prompt", token_count=3200),
                ContextUsageCategory(
                    key="skills",
                    label="Skills",
                    token_count=900,
                    details=[ContextUsageDetail(label="excel", token_count=450)],
                ),
                ContextUsageCategory(key="messages", label="Messages", token_count=2300),
                ContextUsageCategory(
                    key="session_recall",
                    label="Session recall message",
                    token_count=None,
                    char_count=1842,
                ),
                ContextUsageCategory(key="tools", label="Tools", token_count=600),
            ],
            free_tokens=1041576,
        )
        context_usage_service = StubContextUsageService(snapshot)
        loop = ChatLoop(
            agent=self._build_agent(),
            coordinator=StubContextCoordinator(self._build_agent()),
            session=Session.create(agent_id="Pickle", session_id="session-1"),
            console=console,
            input_reader=lambda _: next(submitted_inputs),
            context_usage_service=context_usage_service,
            context_renderer=ContextRenderer(),
        )

        await loop.run()

        rendered = console.export_text()
        # Task 10: /context 展示 ModelContext 观测（不触发旧 usage service）
        self.assertIn("ModelContext", rendered)
        self.assertIn("predicted=true", rendered)
        self.assertEqual(0, len(context_usage_service.calls))

    async def test_session_command_renders_preview(self) -> None:
        output = StringIO()
        console = Console(file=output, force_terminal=False, width=120, record=True)
        submitted_inputs = iter(["/session", "/exit"])
        session_service = FakeSessionService()
        loop = ChatLoop(
            agent=self._build_agent(),
            coordinator=SilentCoordinator(),
            session=Session.create(agent_id="Pickle", session_id="session-1"),
            console=console,
            input_reader=lambda _: next(submitted_inputs),
            session_service=session_service,
        )

        await loop.run()

        rendered = console.export_text()
        self.assertIn("session-1", rendered)
        self.assertIn("runtime reply", rendered)

    async def test_from_config_path_uses_react_max_steps_from_app_config(self) -> None:
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

            loop = ChatLoop.from_config_path(config_path=config_path)

            self.assertEqual(16, loop.coordinator.strategy.max_steps)


if __name__ == "__main__":
    unittest.main()
