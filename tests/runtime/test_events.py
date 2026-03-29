import unittest
from pathlib import Path

from myopenclaw.agent.agent import Agent
from myopenclaw.conversation.message import ToolCall
from myopenclaw.conversation.session import Session
from myopenclaw.llm.config import ModelConfig
from myopenclaw.llm.provider import BaseLLMProvider
from myopenclaw.runtime import FinishReason, GenerateResult, RuntimeEventType, TurnRunner
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


class StubProviderResolver:
    def __init__(self, provider: BaseLLMProvider) -> None:
        self.provider = provider

    def resolve(self, agent: Agent) -> BaseLLMProvider:
        return self.provider


class StubToolResolver:
    def __init__(self, tools: list[BaseTool]) -> None:
        self.tools = tools

    def resolve(self, agent: Agent) -> list[BaseTool]:
        return list(self.tools)


class RuntimeEventTests(unittest.IsolatedAsyncioTestCase):
    async def test_runner_emits_live_events_for_step_tool_and_final_answer(self) -> None:
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
        runner = TurnRunner(
            provider_resolver=StubProviderResolver(
                StubProvider(
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
                )
            ),
            tool_resolver=StubToolResolver([StubTool()]),
            max_steps=4,
        )
        session = Session.create(agent_id="Pickle", session_id="session-1")
        events = []

        async def capture(event) -> None:
            events.append(event)

        result = await runner.run_turn(
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
