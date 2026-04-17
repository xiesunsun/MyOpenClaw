import asyncio
import unittest
from pathlib import Path

from myopenclaw.agents.agent import Agent
from myopenclaw.conversations.message import ToolCall
from myopenclaw.conversations.session import Session
from myopenclaw.providers.base import BaseLLMProvider
from myopenclaw.runs import (
    AgentCoordinator,
    AgentRuntimeContext,
    FinishReason,
    GenerateResult,
    ReActStrategy,
    RuntimeEventType,
)
from myopenclaw.shared.model_config import ModelConfig
from myopenclaw.tools.base import BaseTool, ToolExecutionContext, ToolExecutionResult, ToolSpec


class StubProvider(BaseLLMProvider):
    def __init__(self, responses: list[GenerateResult]) -> None:
        self.responses = list(responses)

    @classmethod
    def from_config(cls, config: ModelConfig) -> "StubProvider":
        raise NotImplementedError

    async def generate(self, request):
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
            context=AgentRuntimeContext(
                agent=agent,
                provider=StubProvider(
                    responses=[
                        GenerateResult(
                            tool_calls=[
                                ToolCall(
                                    id="call-slow",
                                    name="echo",
                                    arguments={"text": "slow", "delay_ms": 40},
                                ),
                                ToolCall(
                                    id="call-fast",
                                    name="echo",
                                    arguments={"text": "fast", "delay_ms": 0},
                                ),
                            ],
                            finish_reason=FinishReason.TOOL_CALLS,
                        ),
                        GenerateResult(
                            text="done",
                            finish_reason=FinishReason.STOP,
                        ),
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

        self.assertEqual("done", result.text)
        self.assertEqual(
            [
                RuntimeEventType.MODEL_STEP_STARTED,
                RuntimeEventType.TOOL_CALL_STARTED,
                RuntimeEventType.TOOL_CALL_STARTED,
                RuntimeEventType.TOOL_CALL_COMPLETED,
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
        self.assertEqual(1, events[2].call_index)
        self.assertEqual(2, events[1].total_calls)
        self.assertEqual(1, events[3].call_index)
        self.assertEqual("fast", events[3].tool_result.content)
        self.assertEqual(0, events[4].call_index)
        self.assertEqual("slow", events[4].tool_result.content)
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
            context=AgentRuntimeContext(
                agent=agent,
                provider=StubProvider(
                    responses=[
                        GenerateResult(
                            tool_calls=[
                                ToolCall(
                                    id="call-1",
                                    name="missing",
                                    arguments={},
                                )
                            ],
                            finish_reason=FinishReason.TOOL_CALLS,
                        ),
                        GenerateResult(text="done"),
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
