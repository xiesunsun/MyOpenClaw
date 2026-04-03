import unittest
from pathlib import Path

from myopenclaw.application.contracts import FinishReason, GenerateRequest, GenerateResult, ModelToolCall
from myopenclaw.application.events import RuntimeEventType
from myopenclaw.application.ports import LLMPort, ToolExecutorPort
from myopenclaw.application.services import ChatService
from myopenclaw.domain.agent import Agent
from myopenclaw.domain.message import ToolCallResult
from myopenclaw.domain.session import Session


class StubProvider(LLMPort):
    def __init__(self, responses: list[GenerateResult]) -> None:
        self.responses = list(responses)

    async def generate(self, request: GenerateRequest) -> GenerateResult:
        return self.responses.pop(0)


class StubToolExecutor(ToolExecutorPort):
    def __init__(self, batches: list[list[ToolCallResult]]) -> None:
        self.batches = list(batches)

    def describe_tools(self, tool_ids: list[str]):
        return []

    async def execute_calls(self, *, tool_calls, agent: Agent, session_id: str):
        return self.batches.pop(0)


class RuntimeEventTests(unittest.IsolatedAsyncioTestCase):
    async def test_runner_emits_batch_aware_events_for_started_and_completed_calls(self) -> None:
        agent = Agent(
            agent_id="Pickle",
            workspace_path=Path("/tmp/pickle"),
            behavior_path=Path("/tmp/pickle/AGENT.md"),
            behavior_instruction="You are Pickle.",
            model_provider="google/gemini",
            model_name="gemini-3-flash-preview",
            tool_ids=["echo"],
        )
        service = ChatService(
            agent=agent,
            session=Session.create(agent_id="Pickle", session_id="session-1"),
            llm=StubProvider(
                responses=[
                    GenerateResult(
                        tool_calls=[
                            ModelToolCall(
                                id="call-slow",
                                name="echo",
                                arguments={"text": "slow", "delay_ms": 40},
                            ),
                            ModelToolCall(
                                id="call-fast",
                                name="echo",
                                arguments={"text": "fast", "delay_ms": 0},
                            ),
                        ],
                        finish_reason=FinishReason.TOOL_CALLS,
                    ),
                    GenerateResult(text="done", finish_reason=FinishReason.STOP),
                ]
            ),
            tool_executor=StubToolExecutor(
                batches=[
                    [
                        ToolCallResult(call_id="call-slow", content="slow"),
                        ToolCallResult(call_id="call-fast", content="fast"),
                    ]
                ]
            ),
            max_steps=4,
        )
        events = []

        async def capture(event) -> None:
            events.append(event)

        result = await service.run_turn("hello", event_handler=capture)

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
        self.assertEqual("slow", events[3].tool_result.content)
        self.assertEqual("fast", events[4].tool_result.content)
        self.assertEqual("done", events[6].text)

    async def test_runner_emits_failed_event_for_erroring_tool_call(self) -> None:
        agent = Agent(
            agent_id="Pickle",
            workspace_path=Path("/tmp/pickle"),
            behavior_path=Path("/tmp/pickle/AGENT.md"),
            behavior_instruction="You are Pickle.",
            model_provider="google/gemini",
            model_name="gemini-3-flash-preview",
            tool_ids=["missing"],
        )
        service = ChatService(
            agent=agent,
            session=Session.create(agent_id="Pickle", session_id="session-1"),
            llm=StubProvider(
                responses=[
                    GenerateResult(
                        tool_calls=[
                            ModelToolCall(
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
            tool_executor=StubToolExecutor(
                batches=[
                    [
                        ToolCallResult(
                            call_id="call-1",
                            content="Tool 'missing' is not available.",
                            is_error=True,
                        )
                    ]
                ]
            ),
            max_steps=2,
        )
        events = []

        async def capture(event) -> None:
            events.append(event)

        await service.run_turn("hello", event_handler=capture)

        failure_event = next(
            event for event in events if event.event_type == RuntimeEventType.TOOL_CALL_FAILED
        )
        self.assertEqual("missing", failure_event.tool_call.name)
        self.assertTrue(failure_event.tool_result.is_error)


if __name__ == "__main__":
    unittest.main()
