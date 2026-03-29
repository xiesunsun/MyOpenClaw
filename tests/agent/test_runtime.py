import unittest
from pathlib import Path

from myopenclaw.agent.agent import Agent
from myopenclaw.agent.definition import AgentDefinition
from myopenclaw.agent.runtime import AgentRuntime
from myopenclaw.conversation.message import MessageRole, ToolCall
from myopenclaw.conversation.session import Session
from myopenclaw.llm.config import ModelConfig
from myopenclaw.llm.provider import BaseLLMProvider
from myopenclaw.runtime_protocols.generation import FinishReason, GenerateRequest, GenerateResult
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


class AgentRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_runtime_appends_messages_and_calls_provider(self) -> None:
        definition = AgentDefinition(
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
        agent = Agent(definition=definition, provider=provider, tools=[])
        session = Session(session_id="session-1", agent_id="Pickle")

        result = await AgentRuntime().run_turn(
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

    async def test_runtime_runs_multi_step_tool_loop_until_final_answer(self) -> None:
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
        agent = Agent(definition=definition, provider=provider, tools=[tool])
        session = Session(session_id="session-1", agent_id="Pickle")

        result = await AgentRuntime(max_steps=4).run_turn(
            agent=agent,
            session=session,
            user_text="hello",
        )

        self.assertEqual("done", result.text)
        self.assertEqual(2, len(provider.requests))
        self.assertEqual(["echo"], [tool_spec.name for tool_spec in provider.requests[0].tools])
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
