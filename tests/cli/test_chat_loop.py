import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
import textwrap
from unittest.mock import Mock

from myopenclaw.application.contracts import (
    ModelToolCall as ToolCall,
    ModelToolCallBatch as ToolCallBatch,
    ModelToolResult as ToolCallResult,
    TurnResult,
)
from myopenclaw.application.events import (
    RuntimeEvent,
    RuntimeEventType,
    ToolCallView,
    ToolResultView,
)
from myopenclaw.domain.agent import Agent
from myopenclaw.domain.metadata import MessageMetadata
from myopenclaw.domain.session import Session
from myopenclaw.interfaces.cli.chat import ChatLoop


class StubChatService:
    def __init__(self, agent: Agent, session: Session | None = None, max_steps: int = 8) -> None:
        self.agent = agent
        self.session = session or Session(session_id="session-1", agent_id=agent.agent_id)
        self.max_steps = max_steps

    async def run_turn(self, text: str, event_handler=None) -> TurnResult:
        self.session.append_user_message(text)
        self.session.append_assistant_message("runtime reply")
        if event_handler is not None:
            await event_handler(
                RuntimeEvent(
                    event_type=RuntimeEventType.ASSISTANT_MESSAGE,
                    text="runtime reply",
                )
            )
        return TurnResult(text="runtime reply", message_count=len(self.session.messages))


class StubToolChatService(StubChatService):
    async def run_turn(self, text: str, event_handler=None) -> TurnResult:
        self.session.append_user_message(text)
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
        self.session.append_assistant_tool_batch(batch)
        if event_handler is not None:
            await event_handler(
                RuntimeEvent(
                    event_type=RuntimeEventType.TOOL_CALL_STARTED,
                    step_index=1,
                    batch_id="batch-1",
                    call_index=0,
                    total_calls=1,
                    tool_call=ToolCallView(
                        id=batch.calls[0].id,
                        name=batch.calls[0].name,
                        arguments=dict(batch.calls[0].arguments),
                    ),
                )
            )
            await event_handler(
                RuntimeEvent(
                    event_type=RuntimeEventType.TOOL_CALL_COMPLETED,
                    step_index=1,
                    batch_id="batch-1",
                    call_index=0,
                    total_calls=1,
                    tool_call=ToolCallView(
                        id=batch.calls[0].id,
                        name=batch.calls[0].name,
                        arguments=dict(batch.calls[0].arguments),
                    ),
                    tool_result=ToolResultView(
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
        return TurnResult(
            text="final reply",
            metadata=MessageMetadata(
                provider="google/gemini",
                model="gemini-3-flash-preview",
            ),
            tool_batches=[batch],
            message_count=len(self.session.messages),
        )


class ChatLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_handle_user_input_delegates_to_coordinator_and_updates_session_count(self) -> None:
        agent = Agent(
            agent_id="Pickle",
            workspace_path=Path("/tmp/pickle"),
            behavior_path=Path("/tmp/pickle/AGENT.md"),
            behavior_instruction="You are Pickle.",
            model_provider="google/gemini",
            model_name="gemini-3-flash-preview",
            tool_ids=[],
        )
        session = Session(session_id="session-1", agent_id="Pickle")
        loop = ChatLoop(
            chat_service=StubChatService(agent=agent, session=session),
        )

        result = await loop.handle_user_input("hello")

        self.assertEqual("runtime reply", result.text)
        self.assertEqual(2, loop._message_count())

    async def test_chat_loop_creates_session_from_conversation_layer(self) -> None:
        agent = Agent(
            agent_id="Pickle",
            workspace_path=Path("/tmp/pickle"),
            behavior_path=Path("/tmp/pickle/AGENT.md"),
            behavior_instruction="You are Pickle.",
            model_provider="google/gemini",
            model_name="gemini-3-flash-preview",
            tool_ids=[],
        )

        loop = ChatLoop(
            chat_service=StubChatService(agent=agent),
        )

        self.assertEqual("Pickle", loop.chat_service.session.agent_id)

    async def test_handle_user_input_renders_tool_batch_progress_before_final_reply(self) -> None:
        agent = Agent(
            agent_id="Pickle",
            workspace_path=Path("/tmp/pickle"),
            behavior_path=Path("/tmp/pickle/AGENT.md"),
            behavior_instruction="You are Pickle.",
            model_provider="google/gemini",
            model_name="gemini-3-flash-preview",
            tool_ids=[],
        )
        session = Session(session_id="session-1", agent_id="Pickle")
        console = Mock()
        loop = ChatLoop(
            chat_service=StubToolChatService(agent=agent, session=session),
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
        agent = Agent(
            agent_id="Pickle",
            workspace_path=Path("/tmp/pickle"),
            behavior_path=Path("/tmp/pickle/AGENT.md"),
            behavior_instruction="You are Pickle.",
            model_provider="google/gemini",
            model_name="gemini-3-flash-preview",
            tool_ids=[],
        )
        session = Session(session_id="session-1", agent_id="Pickle")
        batch = ToolCallBatch(
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
        session.append_assistant_tool_batch(batch)
        console = Mock()
        loop = ChatLoop(
            chat_service=StubChatService(agent=agent, session=session),
            console=console,
        )

        loop.render_turn_output(
            TurnResult(
                text="final reply",
                metadata=MessageMetadata(provider="google/gemini", model="gemini-3-flash-preview"),
                tool_batches=[batch],
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

            from myopenclaw.bootstrap.assembly import BootstrapAssembly

            assembly = BootstrapAssembly.from_config_path(config_path)
            loop = ChatLoop(
                chat_service=assembly.build_chat_service(),
                config_path=config_path,
            )

            self.assertEqual(16, loop.chat_service.max_steps)


if __name__ == "__main__":
    unittest.main()
