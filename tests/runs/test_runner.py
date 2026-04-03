import unittest
from pathlib import Path

from myopenclaw.application.contracts import (
    FinishReason,
    GenerateRequest,
    GenerateResult,
    ModelToolCall,
    TokenUsage,
    ToolSpec,
)
from myopenclaw.application.ports import LLMPort, ToolExecutorPort
from myopenclaw.application.services import ChatService
from myopenclaw.domain.agent import Agent
from myopenclaw.domain.message import ToolCallResult
from myopenclaw.domain.session import Session
from myopenclaw.infrastructure.tools.executor import BuiltinToolExecutor
from myopenclaw.infrastructure.tools.policy import FullAccessPathPolicy


class StubProvider(LLMPort):
    def __init__(self, responses: list[GenerateResult] | None = None) -> None:
        self.requests: list[GenerateRequest] = []
        self.responses = responses or [GenerateResult(text="assistant reply")]

    async def generate(self, request: GenerateRequest) -> GenerateResult:
        self.requests.append(request)
        return self.responses.pop(0)


class StubToolExecutor(ToolExecutorPort):
    def __init__(
        self,
        *,
        tool_specs: list[ToolSpec] | None = None,
        batches: list[list[ToolCallResult]] | None = None,
    ) -> None:
        self.tool_specs = tool_specs or []
        self.batches = batches or []
        self.calls: list[tuple[list[object], str, str]] = []

    def describe_tools(self, tool_ids: list[str]) -> list[ToolSpec]:
        return [tool for tool in self.tool_specs if tool.name in tool_ids]

    async def execute_calls(
        self,
        *,
        tool_calls,
        agent: Agent,
        session_id: str,
    ) -> list[ToolCallResult]:
        self.calls.append((list(tool_calls), agent.agent_id, session_id))
        if self.batches:
            return self.batches.pop(0)
        return []


class ChatServiceTests(unittest.IsolatedAsyncioTestCase):
    def test_builtin_tool_executor_uses_full_access_policy_when_agent_requests_it(self) -> None:
        policy = BuiltinToolExecutor._policy_for_mode("full")

        self.assertIsInstance(policy, FullAccessPathPolicy)

    async def test_runner_appends_messages_and_calls_provider(self) -> None:
        agent = Agent(
            agent_id="Pickle",
            workspace_path=Path("/tmp/pickle"),
            behavior_path=Path("/tmp/pickle/AGENT.md"),
            behavior_instruction="You are Pickle.",
            model_provider="google/gemini",
            model_name="gemini-3-flash-preview",
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
        session = Session.create(agent_id="Pickle", session_id="session-1")
        service = ChatService(
            agent=agent,
            session=session,
            llm=provider,
            tool_executor=StubToolExecutor(),
        )

        result = await service.run_turn("hello")

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
            model_provider="google/gemini",
            model_name="gemini-3-flash-preview",
            tool_ids=["echo"],
        )
        provider = StubProvider(
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
                GenerateResult(
                    text="done",
                    finish_reason=FinishReason.STOP,
                ),
            ]
        )
        tool_executor = StubToolExecutor(
            tool_specs=[
                ToolSpec(
                    name="echo",
                    description="Echo text",
                    input_schema={"type": "object"},
                )
            ],
            batches=[
                [
                    ToolCallResult(call_id="call-slow", content="slow", metadata={"exit_code": 0}),
                    ToolCallResult(call_id="call-fast", content="fast", metadata={"exit_code": 0}),
                ]
            ],
        )
        session = Session.create(agent_id="Pickle", session_id="session-1")
        service = ChatService(
            agent=agent,
            session=session,
            llm=provider,
            tool_executor=tool_executor,
            max_steps=4,
        )

        result = await service.run_turn("hello")

        self.assertEqual("done", result.text)
        self.assertEqual(2, len(provider.requests))
        self.assertEqual(["echo"], [tool_spec.name for tool_spec in provider.requests[0].tools])
        self.assertEqual(1, len(tool_executor.calls))
        self.assertEqual("Pickle", tool_executor.calls[0][1])
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


if __name__ == "__main__":
    unittest.main()
