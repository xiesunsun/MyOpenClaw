import unittest
from pathlib import Path

from myopenclaw.agent.agent import Agent
from myopenclaw.conversation.message import MessageRole, ToolCall
from myopenclaw.conversation.session import Session
from myopenclaw.llm.config import ModelConfig
from myopenclaw.llm.provider import BaseLLMProvider
from myopenclaw.runtime import FinishReason, GenerateRequest, GenerateResult, TurnRunner
from myopenclaw.tools.base import BaseTool, ToolExecutionContext, ToolExecutionResult, ToolSpec


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

    def __init__(self) -> None:
        self.calls: list[tuple[dict[str, object], ToolExecutionContext]] = []

    async def execute(
        self,
        arguments: dict[str, object],
        context: ToolExecutionContext,
    ) -> ToolExecutionResult:
        self.calls.append((arguments, context))
        return ToolExecutionResult(content=str(arguments["text"]))


class StubProviderResolver:
    def __init__(self, provider: BaseLLMProvider) -> None:
        self.provider = provider
        self.calls: list[Agent] = []

    def resolve(self, agent: Agent) -> BaseLLMProvider:
        self.calls.append(agent)
        return self.provider


class StubToolResolver:
    def __init__(self, tools: list[BaseTool]) -> None:
        self.tools = tools
        self.calls: list[Agent] = []

    def resolve(self, agent: Agent) -> list[BaseTool]:
        self.calls.append(agent)
        return list(self.tools)


class TurnRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_runner_appends_messages_and_calls_provider_from_agent_defaults(self) -> None:
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
        provider = StubProvider()
        runner = TurnRunner(
            provider_resolver=StubProviderResolver(provider),
            tool_resolver=StubToolResolver([]),
        )
        session = Session.create(agent_id="Pickle", session_id="session-1")

        result = await runner.run_turn(
            agent=agent,
            session=session,
            user_text="hello",
        )

        self.assertEqual("assistant reply", result.text)
        self.assertEqual(1, len(provider.requests))
        self.assertEqual("You are Pickle.", provider.requests[0].system_instruction)
        self.assertEqual(
            [(MessageRole.USER, "hello")],
            [(message.role, message.content) for message in provider.requests[0].messages],
        )
        self.assertEqual(
            [(MessageRole.USER, "hello"), (MessageRole.ASSISTANT, "assistant reply")],
            [(message.role, message.content) for message in session.messages],
        )
        self.assertEqual("google/gemini", session.messages[1].metadata.provider)
        self.assertEqual("gemini-3-flash-preview", session.messages[1].metadata.model)

    async def test_runner_resolves_tools_from_agent_tool_ids_and_loops_until_final_answer(self) -> None:
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
        tool = StubTool()
        tool_resolver = StubToolResolver([tool])
        runner = TurnRunner(
            provider_resolver=StubProviderResolver(provider),
            tool_resolver=tool_resolver,
            max_steps=4,
        )
        session = Session.create(agent_id="Pickle", session_id="session-1")

        result = await runner.run_turn(
            agent=agent,
            session=session,
            user_text="hello",
        )

        self.assertEqual("done", result.text)
        self.assertEqual(2, len(provider.requests))
        self.assertEqual(["echo"], [tool_spec.name for tool_spec in provider.requests[0].tools])
        self.assertEqual(1, len(tool_resolver.calls))
        self.assertEqual(1, len(tool.calls))
        self.assertEqual("Pickle", tool.calls[0][1].agent_id)
        self.assertEqual(
            [
                MessageRole.USER,
                MessageRole.ASSISTANT,
                MessageRole.TOOL,
                MessageRole.ASSISTANT,
            ],
            [message.role for message in session.messages],
        )
        self.assertEqual("echo", session.messages[1].tool_calls[0].name)
        self.assertEqual("ping", session.messages[2].content)


if __name__ == "__main__":
    unittest.main()
