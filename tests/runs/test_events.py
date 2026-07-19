import asyncio
import unittest
from pathlib import Path

from myopenclaw.agents.agent import Agent
from myopenclaw.context.assembler import ContextAssembler
from myopenclaw.context.model_context import ModelContext
from myopenclaw.conversations.agent_message import AssistantMessage
from myopenclaw.conversations.content_blocks import TextContent, ToolCallContent
from myopenclaw.conversations.session import Session
from myopenclaw.providers.base import BaseLLMProvider
from myopenclaw.runs import AgentCoordinator, ReActStrategy, RuntimeEventType
from myopenclaw.runs.dependencies import RunDependencies
from myopenclaw.shared.model_config import ModelConfig
from myopenclaw.tools.base import BaseTool, ToolExecutionContext, ToolExecutionResult, ToolSpec


def _assistant_text(message: AssistantMessage) -> str:
    return "\n".join(
        block.text for block in message.content if isinstance(block, TextContent) and block.text
    )


class StubProvider(BaseLLMProvider):
    def __init__(self, responses: list[AssistantMessage]) -> None:
        self.responses = list(responses)

    @classmethod
    def from_config(cls, config: ModelConfig) -> "StubProvider":
        raise NotImplementedError

    async def generate(self, context: ModelContext) -> AssistantMessage:
        return self.responses.pop(0)


class DelayEchoTool(BaseTool):
    spec = ToolSpec(
        name="echo",
        description="Echo text",
        input_schema={
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "delay_ms": {"type": "integer"},
            },
            "required": ["text"],
        },
    )

    async def execute(
        self,
        arguments: dict[str, object],
        context: ToolExecutionContext,
    ) -> ToolExecutionResult:
        await asyncio.sleep(int(arguments.get("delay_ms", 0)) / 1000)
        return ToolExecutionResult(content=str(arguments["text"]))


def _deps(*, agent: Agent, provider: BaseLLMProvider, tools: list[BaseTool]) -> RunDependencies:
    return RunDependencies(
        agent=agent,
        provider=provider,
        tools=tools,
        context_assembler=ContextAssembler(),
    )


class RuntimeEventTests(unittest.IsolatedAsyncioTestCase):
    async def test_runner_emits_batch_aware_events_for_started_and_completed_calls(self) -> None:
        agent = Agent(
            agent_id="Pickle",
            workspace_path=Path("/tmp/pickle"),
            behavior_path=Path("/tmp/pickle/AGENT.md"),
            behavior_instruction="You are Pickle.",
            model_config=ModelConfig(
                provider="google/gemini",
                model="gemini-3-flash-preview",
            ),
            tool_ids=["echo"],
        )
        coordinator = AgentCoordinator(
            strategy=ReActStrategy(max_steps=4),
            deps=_deps(
                agent=agent,
                provider=StubProvider(
                    responses=[
                        AssistantMessage(
                            content=[
                                ToolCallContent(
                                    id="call-slow",
                                    name="echo",
                                    arguments={"text": "slow", "delay_ms": 40},
                                ),
                                ToolCallContent(
                                    id="call-fast",
                                    name="echo",
                                    arguments={"text": "fast", "delay_ms": 0},
                                ),
                            ]
                        ),
                        AssistantMessage(content=[TextContent(text="done")]),
                    ]
                ),
                tools=[DelayEchoTool()],
            ),
        )
        session = Session.create(agent_id="Pickle", session_id="session-1")
        events = []

        async def capture(event) -> None:
            events.append(event)

        result = await coordinator.run_turn(
            agent=agent,
            session=session,
            user_text="hello",
            event_handler=capture,
        )

        self.assertEqual("done", _assistant_text(result))
        # Task 9: tools run serially for PreToolUse control points
        self.assertEqual(
            [
                RuntimeEventType.MODEL_STEP_STARTED,
                RuntimeEventType.TOOL_CALL_STARTED,
                RuntimeEventType.TOOL_CALL_COMPLETED,
                RuntimeEventType.TOOL_CALL_STARTED,
                RuntimeEventType.TOOL_CALL_COMPLETED,
                RuntimeEventType.MODEL_STEP_STARTED,
                RuntimeEventType.ASSISTANT_MESSAGE,
            ],
            [event.event_type for event in events],
        )
        batch_id = events[1].batch_id
        self.assertIsNotNone(batch_id)
        self.assertEqual(batch_id, events[2].batch_id)
        self.assertEqual(batch_id, events[3].batch_id)
        self.assertEqual(0, events[1].call_index)
        self.assertEqual(0, events[2].call_index)
        self.assertEqual("slow", events[2].tool_result.content)
        self.assertEqual(1, events[3].call_index)
        self.assertEqual(1, events[4].call_index)
        self.assertEqual("fast", events[4].tool_result.content)
        self.assertEqual("done", events[6].text)

    async def test_runner_emits_failed_event_for_erroring_tool_call(self) -> None:
        agent = Agent(
            agent_id="Pickle",
            workspace_path=Path("/tmp/pickle"),
            behavior_path=Path("/tmp/pickle/AGENT.md"),
            behavior_instruction="You are Pickle.",
            model_config=ModelConfig(
                provider="google/gemini",
                model="gemini-3-flash-preview",
            ),
            tool_ids=["missing"],
        )
        coordinator = AgentCoordinator(
            strategy=ReActStrategy(max_steps=2),
            deps=_deps(
                agent=agent,
                provider=StubProvider(
                    responses=[
                        AssistantMessage(
                            content=[
                                ToolCallContent(
                                    id="call-1",
                                    name="missing",
                                    arguments={},
                                )
                            ]
                        ),
                        AssistantMessage(content=[TextContent(text="done")]),
                    ]
                ),
                tools=[],
            ),
        )
        session = Session.create(agent_id="Pickle", session_id="session-1")
        events = []

        async def capture(event) -> None:
            events.append(event)

        await coordinator.run_turn(
            agent=agent,
            session=session,
            user_text="hello",
            event_handler=capture,
        )

        failure_event = next(
            event for event in events if event.event_type == RuntimeEventType.TOOL_CALL_FAILED
        )
        self.assertEqual("missing", failure_event.tool_call.name)
        self.assertTrue(failure_event.tool_result.is_error)


if __name__ == "__main__":
    unittest.main()
