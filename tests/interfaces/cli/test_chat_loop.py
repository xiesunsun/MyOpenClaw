import unittest
from pathlib import Path
from unittest.mock import Mock

from myopenclaw.agent.agent import Agent
from myopenclaw.conversation.message import ToolCall
from myopenclaw.conversation.metadata import MessageMetadata
from myopenclaw.conversation.session import Session
from myopenclaw.interfaces.cli.chat import ChatLoop
from myopenclaw.llm.config import ModelConfig
from myopenclaw.runtime import GenerateResult, RuntimeEvent, RuntimeEventType


class StubRuntime:
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


class StubToolRuntime:
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
        session.append_assistant_message(
            tool_calls=[
                ToolCall(
                    id="call-1",
                    name="read",
                    arguments={"path": "/tmp/" + "very-long-segment/" * 12 + "file.txt"},
                )
            ]
        )
        if event_handler is not None:
            await event_handler(
                RuntimeEvent(
                    event_type=RuntimeEventType.TOOL_CALL_STARTED,
                    tool_call=ToolCall(
                        id="call-1",
                        name="read",
                        arguments={"path": "/tmp/" + "very-long-segment/" * 12 + "file.txt"},
                    ),
                )
            )
        session.append_tool_result(
            content="file content " * 80,
            tool_call_id="call-1",
            tool_name="read",
        )
        if event_handler is not None:
            from myopenclaw.tools.base import ToolExecutionResult

            await event_handler(
                RuntimeEvent(
                    event_type=RuntimeEventType.TOOL_CALL_COMPLETED,
                    tool_call=ToolCall(
                        id="call-1",
                        name="read",
                        arguments={"path": "/tmp/" + "very-long-segment/" * 12 + "file.txt"},
                    ),
                    tool_result=ToolExecutionResult(content="file content " * 80),
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
    async def test_handle_user_input_delegates_to_runtime_and_updates_session_count(self) -> None:
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
            runtime=StubRuntime(),
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
            runtime=StubRuntime(),
        )

        self.assertEqual("Pickle", loop.session.agent_id)

    async def test_handle_user_input_renders_tool_activity_before_final_reply(self) -> None:
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
            runtime=StubToolRuntime(),
            session=session,
            console=console,
        )

        result = await loop.handle_user_input(
            "hello",
            event_handler=loop.create_event_handler(),
        )

        titles = [call.args[0].title for call in console.print.call_args_list]
        tool_call_render = str(console.print.call_args_list[1].args[0].renderable)
        tool_result_render = str(console.print.call_args_list[2].args[0].renderable)

        self.assertEqual("final reply", result.text)
        self.assertEqual(["Thinking", "Tool Call", "Tool Result", "Assistant"], titles)
        self.assertIn("read(", tool_call_render)
        self.assertIn("...", tool_call_render)
        self.assertLess(len(tool_call_render), 180)
        self.assertIn("...", tool_result_render)
        self.assertLess(len(tool_result_render), 220)


if __name__ == "__main__":
    unittest.main()
