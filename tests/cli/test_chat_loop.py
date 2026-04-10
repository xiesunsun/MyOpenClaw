import unittest
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
import textwrap
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from myopenclaw.agents.agent import Agent
from myopenclaw.cli.context_renderer import ContextRenderer
from myopenclaw.context import ContextRuntimeStore
from myopenclaw.conversations.message import ToolCall, ToolCallBatch, ToolCallResult
from myopenclaw.conversations.metadata import MessageMetadata
from myopenclaw.conversations.session import Session
from myopenclaw.cli.chat import ChatLoop
from myopenclaw.runs.context_usage import (
    ContextUsageCategory,
    ContextUsageDetail,
    ContextUsageSnapshot,
)
from myopenclaw.runs import GenerateResult, RuntimeEvent, RuntimeEventType
from myopenclaw.shared.model_config import ModelConfig
from myopenclaw.tools.base import ToolExecutionResult
from rich.console import Console


class StubCoordinator:
    async def run_turn(
        self,
        *,
        agent: Agent,
        session: Session,
        user_text: str,
        event_handler=None,
    ) -> GenerateResult:
        session.append_user_message(user_text)
        session.append_assistant_message("runtime reply")
        if event_handler is not None:
            await event_handler(
                RuntimeEvent(
                    event_type=RuntimeEventType.ASSISTANT_MESSAGE,
                    text="runtime reply",
                )
            )
        return GenerateResult(text="runtime reply")


class SilentCoordinator:
    async def run_turn(
        self,
        *,
        agent: Agent,
        session: Session,
        user_text: str,
        event_handler=None,
    ) -> GenerateResult:
        session.append_user_message(user_text)
        session.append_assistant_message("runtime reply")
        return GenerateResult(text="runtime reply")


class StubToolCoordinator:
    async def run_turn(
        self,
        *,
        agent: Agent,
        session: Session,
        user_text: str,
        event_handler=None,
    ) -> GenerateResult:
        session.append_user_message(user_text)
        if event_handler is not None:
            await event_handler(
                RuntimeEvent(
                    event_type=RuntimeEventType.MODEL_STEP_STARTED,
                    step_index=1,
                )
            )
        batch = ToolCallBatch(
            batch_id="batch-1",
            step_index=1,
            calls=[
                ToolCall(
                    id="call-1",
                    name="read_file",
                    arguments={"path": "/tmp/" + "very-long-segment/" * 12 + "file.txt"},
                )
            ],
            results=[
                ToolCallResult(
                    call_id="call-1",
                    content="file content " * 80,
                    metadata={
                        "cwd": "/tmp/workspace",
                        "exit_code": 0,
                        "shell_status": "ready",
                    },
                )
            ],
        )
        session.append_assistant_tool_batch(batch)
        if event_handler is not None:
            await event_handler(
                RuntimeEvent(
                    event_type=RuntimeEventType.TOOL_CALL_STARTED,
                    step_index=1,
                    batch_id="batch-1",
                    call_index=0,
                    total_calls=1,
                    tool_call=batch.calls[0],
                )
            )
            await event_handler(
                RuntimeEvent(
                    event_type=RuntimeEventType.TOOL_CALL_COMPLETED,
                    step_index=1,
                    batch_id="batch-1",
                    call_index=0,
                    total_calls=1,
                    tool_call=batch.calls[0],
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
        return GenerateResult(
            text="final reply",
            metadata=MessageMetadata(
                provider="google/gemini",
                model="gemini-3-flash-preview",
            ),
        )


class StubContextCoordinator:
    def __init__(self, agent: Agent) -> None:
        self.context = Mock(
            agent=agent,
            provider=Mock(),
            tools=[],
            conversation_context_service=Mock(
                build_snapshot=Mock(
                    return_value=SimpleNamespace(messages=[])
                )
            ),
            context_runtime_store=ContextRuntimeStore(),
        )

    async def run_turn(
        self,
        *,
        agent: Agent,
        session: Session,
        user_text: str,
        event_handler=None,
    ) -> GenerateResult:
        raise AssertionError("run_turn should not be called")


class StubContextUsageService:
    def __init__(self, snapshot: ContextUsageSnapshot) -> None:
        self.snapshot = snapshot
        self.calls: list[tuple[Agent, object, Session, list[object] | None]] = []

    async def build(
        self,
        *,
        agent: Agent,
        context: object,
        session: Session,
        effective_messages=None,
    ) -> ContextUsageSnapshot:
        self.calls.append((agent, context, session, effective_messages))
        return self.snapshot


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
        session = Session(session_id="session-1", agent_id="Pickle")
        loop = ChatLoop(
            agent=agent,
            coordinator=StubCoordinator(),
            session=session,
        )

        result = await loop.handle_user_input("hello")

        self.assertEqual("runtime reply", result.text)
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
        session = Session(session_id="session-1", agent_id="Pickle")
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

        self.assertEqual("final reply", result.text)
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
        session = Session(session_id="session-1", agent_id="Pickle")
        session.append_assistant_tool_batch(
            ToolCallBatch(
                batch_id="batch-1",
                step_index=1,
                calls=[
                    ToolCall(
                        id="call-1",
                        name="read_file",
                        arguments={"path": "file.txt"},
                    )
                ],
                results=[
                    ToolCallResult(
                        call_id="call-1",
                        content="hello world",
                        metadata={"exit_code": 0},
                    )
                ],
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
            GenerateResult(
                text="final reply",
                metadata=MessageMetadata(provider="google/gemini", model="gemini-3-flash-preview"),
            ),
            start_index=0,
        )

        titles = [call.args[0].title for call in console.print.call_args_list]
        self.assertEqual(["Tool", "Assistant"], titles)
        replay_render = str(console.print.call_args_list[0].args[0].renderable)
        self.assertIn("read_file(path='file.txt')", replay_render)
        self.assertIn("status: ok", replay_render)
        self.assertIn("result: hello world", replay_render)
        self.assertNotIn("meta:", replay_render)

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
            session=Session(session_id="session-1", agent_id="Pickle"),
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
            session=Session(session_id="session-1", agent_id="Pickle"),
            console=console,
            input_reader=lambda _: next(submitted_inputs),
        )

        await loop.run()

        titles = [getattr(call.args[0], "title", None) for call in console.print.call_args_list]
        self.assertEqual(1, titles.count("Assistant"))
        self.assertNotIn("You", titles)

    async def test_help_lists_context_command(self) -> None:
        output = StringIO()
        console = Console(file=output, force_terminal=False, width=120, record=True)
        submitted_inputs = iter(["/help", "/exit"])
        loop = ChatLoop(
            agent=self._build_agent(),
            coordinator=SilentCoordinator(),
            session=Session(session_id="session-1", agent_id="Pickle"),
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
            session=Session(session_id="session-1", agent_id="Pickle"),
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
                ContextUsageCategory(key="tools", label="Tools", token_count=600),
            ],
            free_tokens=1041576,
        )
        context_usage_service = StubContextUsageService(snapshot)
        loop = ChatLoop(
            agent=self._build_agent(),
            coordinator=StubContextCoordinator(self._build_agent()),
            session=Session(session_id="session-1", agent_id="Pickle"),
            console=console,
            input_reader=lambda _: next(submitted_inputs),
            context_usage_service=context_usage_service,
            context_renderer=ContextRenderer(),
        )

        await loop.run()

        rendered = console.export_text()
        self.assertIn("Context Usage", rendered)
        self.assertIn("Estimated usage by category", rendered)
        self.assertIn("System prompt", rendered)
        self.assertIn("Skills", rendered)
        self.assertIn("Messages", rendered)
        self.assertIn("Tools", rendered)
        self.assertIn("Free space", rendered)
        self.assertIn("Skills breakdown", rendered)
        self.assertEqual(1, len(context_usage_service.calls))

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
