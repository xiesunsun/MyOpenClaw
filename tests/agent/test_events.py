import unittest
from pathlib import Path

from myopenclaw.agent.agent import Agent
from myopenclaw.agent.definition import AgentDefinition
from myopenclaw.agent.events import RuntimeEventType
from myopenclaw.agent.runtime import AgentRuntime
from myopenclaw.conversation.message import ToolCall
from myopenclaw.conversation.session import Session
from myopenclaw.llm.config import ModelConfig
from myopenclaw.llm.provider import BaseLLMProvider
from myopenclaw.runtime_protocols.generation import FinishReason, GenerateResult
from myopenclaw.tools.base import BaseTool, ToolExecutionContext, ToolExecutionResult, ToolSpec


class StubProvider(BaseLLMProvider):
    def __init__(self, responses: list[GenerateResult]) -> None:
        self.responses = list(responses)

    @classmethod
    def from_config(cls, config: ModelConfig) -> "StubProvider":
        raise NotImplementedError

    async def generate(self, request):
        return self.responses.pop(0)


class StubTool(BaseTool):
    spec = ToolSpec(
        name="echo",
        description="Echo text",
        input_schema={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    )

    async def execute(
        self,
        arguments: dict[str, object],
        context: ToolExecutionContext,
    ) -> ToolExecutionResult:
        return ToolExecutionResult(content=str(arguments["text"]))


class RuntimeEventTests(unittest.IsolatedAsyncioTestCase):
    async def test_runtime_emits_live_events_for_step_tool_and_final_answer(self) -> None:
        definition = AgentDefinition(
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
        agent = Agent(
            definition=definition,
            provider=StubProvider(
                responses=[
                    GenerateResult(
                        tool_calls=[
                            ToolCall(
                                id="call-1",
                                name="echo",
                                arguments={"text": "ping"},
                            )
                        ],
                        finish_reason=FinishReason.TOOL_CALLS,
                    ),
                    GenerateResult(
                        text="done",
                        finish_reason=FinishReason.STOP,
                    ),
                ]
            ),
            tools=[StubTool()],
        )
        session = Session(session_id="session-1", agent_id="Pickle")
        events = []

        async def capture(event) -> None:
            events.append(event)

        result = await AgentRuntime(max_steps=4).run_turn(
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
                RuntimeEventType.TOOL_CALL_COMPLETED,
                RuntimeEventType.MODEL_STEP_STARTED,
                RuntimeEventType.ASSISTANT_MESSAGE,
            ],
            [event.event_type for event in events],
        )
        self.assertEqual(1, events[0].step_index)
        self.assertEqual("echo", events[1].tool_call.name)
        self.assertEqual("ping", events[2].tool_result.content)
        self.assertEqual("done", events[4].text)


if __name__ == "__main__":
    unittest.main()
