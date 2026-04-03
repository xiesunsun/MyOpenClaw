import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
import textwrap
from unittest.mock import Mock

from myopenclaw.agent.agent import Agent
from myopenclaw.conversation.message import ToolCall, ToolCallBatch, ToolCallResult
from myopenclaw.conversation.metadata import MessageMetadata
from myopenclaw.conversation.session import Session
from myopenclaw.interfaces.cli.chat import ChatLoop
from myopenclaw.llm.config import ModelConfig
from myopenclaw.runtime import GenerateResult, RuntimeEvent, RuntimeEventType
from myopenclaw.tools.base import ToolExecutionResult


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
                    name="read",
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


class ChatLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_handle_user_input_delegates_to_coordinator_and_updates_session_count(self) -> None:
        agent = Agent(
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
        agent = Agent(
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

        loop = ChatLoop(
            agent=agent,
            coordinator=StubCoordinator(),
        )

        self.assertEqual("Pickle", loop.session.agent_id)

    async def test_handle_user_input_renders_tool_batch_progress_before_final_reply(self) -> None:
        agent = Agent(
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
        self.assertIn("read(path=", started_render)
        self.assertIn("status: running", started_render)
        self.assertNotIn("step:", started_render)
        self.assertIn("read(path=", completed_render)
        self.assertIn("status: ok", completed_render)
        self.assertIn("result: file content", completed_render)
        self.assertNotIn("meta:", completed_render)

    async def test_render_turn_output_replays_assistant_tool_batch(self) -> None:
        agent = Agent(
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
        session = Session(session_id="session-1", agent_id="Pickle")
        session.append_assistant_tool_batch(
            ToolCallBatch(
                batch_id="batch-1",
                step_index=1,
                calls=[
                    ToolCall(
                        id="call-1",
                        name="read",
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
        self.assertIn("read(path='file.txt')", replay_render)
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

            loop = ChatLoop.from_config_path(config_path=config_path)

            self.assertEqual(16, loop.coordinator.strategy.max_steps)


if __name__ == "__main__":
    unittest.main()
