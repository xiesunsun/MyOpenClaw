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
    GenerateRequest,
    GenerateResult,
    ReActStrategy,
    TokenUsage,
)
from myopenclaw.shared.model_config import ModelConfig
from myopenclaw.tools.base import BaseTool, ToolExecutionContext, ToolExecutionResult, ToolSpec
from myopenclaw.tools.policy import FullAccessPathPolicy


class StubProvider(BaseLLMProvider):
    def __init__(self, responses: list[GenerateResult] | None = None) -> None:
        self.requests: list[GenerateRequest] = []
        self.responses = responses or [GenerateResult(text="assistant reply")]

    @classmethod
    def from_config(cls, config: ModelConfig) -> "StubProvider":
        return cls()

    async def generate(self, request: GenerateRequest) -> GenerateResult:
        self.requests.append(request)
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

    def __init__(self) -> None:
        self.calls: list[tuple[dict[str, object], ToolExecutionContext]] = []

    async def execute(
        self,
        arguments: dict[str, object],
        context: ToolExecutionContext,
    ) -> ToolExecutionResult:
        self.calls.append((arguments, context))
        await asyncio.sleep(int(arguments.get("delay_ms", 0)) / 1000)
        return ToolExecutionResult(
            content=str(arguments["text"]),
            metadata={"exit_code": 0},
        )


class ReActStrategyTests(unittest.IsolatedAsyncioTestCase):
    def test_runtime_context_uses_full_access_policy_when_agent_requests_it(self) -> None:
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
            file_access_mode="full",
        )
        provider = StubProvider()

        context = AgentRuntimeContext(agent=agent, provider=provider, tools=[])

        self.assertIsInstance(context.file_access_policy, FullAccessPathPolicy)

    async def test_runner_appends_messages_and_calls_provider(self) -> None:
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
        provider = StubProvider(
            responses=[
                GenerateResult(
                    text="assistant reply",
                    provider_finish_reason="STOP",
                    provider_finish_message="Model stopped normally.",
                    provider_response_id="resp-1",
                    provider_model_version="gemini-3-flash-preview-001",
                    usage=TokenUsage(input_tokens=3, output_tokens=5, total_tokens=8),
                )
            ]
        )
        coordinator = AgentCoordinator(
            strategy=ReActStrategy(),
            context=AgentRuntimeContext(agent=agent, provider=provider, tools=[]),
        )
        session = Session.create(agent_id="Pickle", session_id="session-1")

        result = await coordinator.run_turn(
            agent=agent,
            session=session,
            user_text="hello",
        )

        self.assertEqual("assistant reply", result.text)
        self.assertEqual(1, len(provider.requests))
        self.assertEqual("You are Pickle.", provider.requests[0].system_instruction)
        self.assertEqual("hello", provider.requests[0].messages[0].content)
        self.assertEqual(["user", "assistant"], [message.role.value for message in session.messages])
        self.assertEqual("google/gemini", session.messages[1].metadata.provider)
        self.assertEqual("gemini-3-flash-preview", session.messages[1].metadata.model)
        self.assertEqual(8, session.messages[1].metadata.total_tokens)
        self.assertEqual("STOP", session.messages[1].metadata.provider_finish_reason)
        self.assertEqual("resp-1", session.messages[1].metadata.provider_response_id)

    async def test_runner_persists_tool_batch_results_in_call_order(self) -> None:
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
        provider = StubProvider(
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
        )
        tool = DelayEchoTool()
        coordinator = AgentCoordinator(
            strategy=ReActStrategy(max_steps=4),
            context=AgentRuntimeContext(agent=agent, provider=provider, tools=[tool]),
        )
        session = Session.create(agent_id="Pickle", session_id="session-1")

        result = await coordinator.run_turn(
            agent=agent,
            session=session,
            user_text="hello",
        )

        self.assertEqual("done", result.text)
        self.assertEqual(2, len(provider.requests))
        self.assertEqual(["echo"], [tool_spec.name for tool_spec in provider.requests[0].tools])
        self.assertEqual(2, len(tool.calls))
        self.assertEqual("Pickle", tool.calls[0][1].agent_id)
        self.assertEqual(
            ["user", "assistant", "assistant"],
            [message.role.value for message in session.messages],
        )
        batch = session.messages[1].tool_call_batch
        self.assertIsNotNone(batch)
        self.assertEqual(["call-slow", "call-fast"], [call.id for call in batch.calls])
        self.assertEqual(["call-slow", "call-fast"], [result.call_id for result in batch.results])
        self.assertEqual(["slow", "fast"], [result.content for result in batch.results])
        second_request_batch = provider.requests[1].messages[1].tool_call_batch
        self.assertEqual(["call-slow", "call-fast"], [result.call_id for result in second_request_batch.results])

    async def test_runner_summarizes_completed_turn_history_on_next_turn(self) -> None:
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
        provider = StubProvider(
            responses=[
                GenerateResult(
                    tool_calls=[
                        ToolCall(
                            id="call-1",
                            name="echo",
                            arguments={"text": "history"},
                        )
                    ],
                    finish_reason=FinishReason.TOOL_CALLS,
                ),
                GenerateResult(
                    text="first answer",
                    finish_reason=FinishReason.STOP,
                ),
                GenerateResult(
                    text="second answer",
                    finish_reason=FinishReason.STOP,
                ),
            ]
        )
        tool = DelayEchoTool()
        coordinator = AgentCoordinator(
            strategy=ReActStrategy(max_steps=4),
            context=AgentRuntimeContext(agent=agent, provider=provider, tools=[tool]),
        )
        session = Session.create(agent_id="Pickle", session_id="session-1")

        first_result = await coordinator.run_turn(
            agent=agent,
            session=session,
            user_text="first user",
        )
        second_result = await coordinator.run_turn(
            agent=agent,
            session=session,
            user_text="second user",
        )

        self.assertEqual("first answer", first_result.text)
        self.assertEqual("second answer", second_result.text)
        self.assertEqual(3, len(provider.requests))
        history_request = provider.requests[2]
        self.assertEqual(
            ["first user", 'Tool step summary:\n- echo args={"text": "history"} -> result=history', "first answer", "second user"],
            [message.content for message in history_request.messages],
        )
        self.assertTrue(all(message.tool_call_batch is None for message in history_request.messages[:-1]))


if __name__ == "__main__":
    unittest.main()
