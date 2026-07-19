import asyncio
import unittest
from pathlib import Path

from myopenclaw.agents.agent import Agent
from myopenclaw.context.assembler import ContextAssembler
from myopenclaw.context.model_context import ModelContext
from myopenclaw.conversations.agent_message import (
    AssistantMessage,
    ModelResponseMetadata,
    ModelUsage,
)
from myopenclaw.conversations.content_blocks import TextContent, ThinkingContent, ToolCallContent
from myopenclaw.conversations.session import Session
from myopenclaw.providers.base import BaseLLMProvider
from myopenclaw.runs import AgentCoordinator, AgentRuntimeContext, ReActStrategy
from myopenclaw.runs.dependencies import RunDependencies
from myopenclaw.shared.model_config import ModelConfig
from myopenclaw.tools.base import BaseTool, ToolExecutionContext, ToolExecutionResult, ToolSpec
from myopenclaw.tools.policy import FullAccessPathPolicy


def _assistant_text(message: AssistantMessage) -> str:
    return "\n".join(
        block.text for block in message.content if isinstance(block, TextContent) and block.text
    )


def _entry_roles(session: Session) -> list[str | None]:
    return [entry.payload.get("role") for entry in session.active_path()]


class StubProvider(BaseLLMProvider):
    def __init__(self, responses: list[AssistantMessage] | None = None) -> None:
        self.contexts: list[ModelContext] = []
        self.responses = list(responses or [AssistantMessage(content=[TextContent(text="assistant reply")])])

    @classmethod
    def from_config(cls, config: ModelConfig) -> "StubProvider":
        return cls()

    async def generate(self, context: ModelContext) -> AssistantMessage:
        self.contexts.append(context)
        return self.responses.pop(0)


class HangingProvider(BaseLLMProvider):
    @classmethod
    def from_config(cls, config: ModelConfig) -> "HangingProvider":
        return cls()

    async def generate(self, context: ModelContext) -> AssistantMessage:
        await asyncio.sleep(60)
        raise AssertionError("generate should time out before finishing")


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


def _deps(
    *,
    agent: Agent,
    provider: BaseLLMProvider,
    tools: list[BaseTool] | None = None,
    unit_window: int = 5,
) -> RunDependencies:
    return RunDependencies(
        agent=agent,
        provider=provider,
        tools=tools or [],
        context_assembler=ContextAssembler(),
        unit_window=unit_window,
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
                AssistantMessage(
                    content=[TextContent(text="assistant reply")],
                    metadata=ModelResponseMetadata(
                        provider="google/gemini",
                        model="gemini-3-flash-preview",
                        finish_reason="STOP",
                        finish_message="Model stopped normally.",
                        provider_response_id="resp-1",
                        provider_model_version="gemini-3-flash-preview-001",
                        usage=ModelUsage(input_tokens=3, output_tokens=5, total_tokens=8),
                    ),
                )
            ]
        )
        coordinator = AgentCoordinator(
            strategy=ReActStrategy(),
            deps=_deps(agent=agent, provider=provider),
        )
        session = Session.create(agent_id="Pickle", session_id="session-1")

        result = await coordinator.run_turn(
            agent=agent,
            session=session,
            user_text="hello",
        )

        self.assertEqual("assistant reply", _assistant_text(result))
        self.assertEqual(1, len(provider.contexts))
        self.assertEqual("You are Pickle.", provider.contexts[0].system.as_text())
        self.assertEqual(1, len(provider.contexts[0].messages))
        user_msg = provider.contexts[0].messages[0]
        self.assertEqual("user", user_msg.role)
        self.assertEqual("hello", user_msg.content[0].text)
        self.assertEqual(["user", "assistant"], _entry_roles(session))
        assistant_payload = session.active_path()[1].payload
        metadata = assistant_payload.get("metadata") or {}
        self.assertEqual("google/gemini", metadata.get("provider"))
        self.assertEqual("gemini-3-flash-preview", metadata.get("model"))
        usage = metadata.get("usage") or {}
        self.assertEqual(8, usage.get("total_tokens"))
        self.assertEqual("STOP", metadata.get("finish_reason"))
        self.assertEqual("resp-1", metadata.get("provider_response_id"))

    async def test_runner_persists_provider_thinking_blocks_on_assistant_messages(self) -> None:
        agent = Agent(
            agent_id="Pickle",
            workspace_path=Path("/tmp/pickle"),
            behavior_path=Path("/tmp/pickle/AGENT.md"),
            behavior_instruction="You are Pickle.",
            model_config=ModelConfig(
                provider="anthropic",
                model="claude-opus-4-7",
            ),
            tool_ids=[],
        )
        provider = StubProvider(
            responses=[
                AssistantMessage(
                    content=[
                        ThinkingContent(text="internal", signature="sig-1"),
                        TextContent(text="assistant reply"),
                    ]
                )
            ]
        )
        coordinator = AgentCoordinator(
            strategy=ReActStrategy(),
            deps=_deps(agent=agent, provider=provider),
        )
        session = Session.create(agent_id="Pickle", session_id="session-1")

        await coordinator.run_turn(
            agent=agent,
            session=session,
            user_text="hello",
        )

        contents = session.active_path()[1].payload.get("content") or []
        thinking = [block for block in contents if block.get("type") == "thinking"]
        self.assertEqual(
            [{"type": "thinking", "text": "internal", "signature": "sig-1"}],
            thinking,
        )

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
                AssistantMessage(
                    content=[
                        ThinkingContent(text="first", signature="sig-1"),
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
        )
        tool = DelayEchoTool()
        coordinator = AgentCoordinator(
            strategy=ReActStrategy(max_steps=4),
            deps=_deps(agent=agent, provider=provider, tools=[tool]),
        )
        session = Session.create(agent_id="Pickle", session_id="session-1")

        result = await coordinator.run_turn(
            agent=agent,
            session=session,
            user_text="hello",
        )

        self.assertEqual("done", _assistant_text(result))
        self.assertEqual(2, len(provider.contexts))
        self.assertEqual(["echo"], [tool_def.name for tool_def in provider.contexts[0].tools])
        self.assertEqual(2, len(tool.calls))
        self.assertEqual("Pickle", tool.calls[0][1].agent_id)
        self.assertEqual(
            ["user", "assistant", "tool", "tool", "assistant"],
            _entry_roles(session),
        )
        assistant_payload = session.active_path()[1].payload
        tool_calls = [
            block
            for block in (assistant_payload.get("content") or [])
            if block.get("type") == "tool_call"
        ]
        self.assertEqual(["call-slow", "call-fast"], [call["id"] for call in tool_calls])
        tool_results = [
            entry.payload
            for entry in session.active_path()
            if entry.payload.get("role") == "tool"
        ]
        self.assertEqual(["call-slow", "call-fast"], [item.get("tool_call_id") for item in tool_results])
        self.assertEqual(
            ["slow", "fast"],
            [
                next(
                    block.get("text")
                    for block in (item.get("content") or [])
                    if block.get("type") == "text"
                )
                for item in tool_results
            ],
        )
        thinking = [
            block
            for block in (assistant_payload.get("content") or [])
            if block.get("type") == "thinking"
        ]
        self.assertEqual(
            [{"type": "thinking", "text": "first", "signature": "sig-1"}],
            thinking,
        )
        second_ctx_roles = [message.role for message in provider.contexts[1].messages]
        self.assertIn("tool", second_ctx_roles)
        tool_msgs = [m for m in provider.contexts[1].messages if m.role == "tool"]
        self.assertEqual(["call-slow", "call-fast"], [m.tool_call_id for m in tool_msgs])

    async def test_runner_keeps_recent_turn_history_raw_on_next_turn(self) -> None:
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
                AssistantMessage(
                    content=[
                        ToolCallContent(
                            id="call-1",
                            name="echo",
                            arguments={"text": "history"},
                        )
                    ]
                ),
                AssistantMessage(content=[TextContent(text="first answer")]),
                AssistantMessage(content=[TextContent(text="second answer")]),
            ]
        )
        tool = DelayEchoTool()
        coordinator = AgentCoordinator(
            strategy=ReActStrategy(max_steps=4),
            deps=_deps(agent=agent, provider=provider, tools=[tool], unit_window=10),
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

        self.assertEqual("first answer", _assistant_text(first_result))
        self.assertEqual("second answer", _assistant_text(second_result))
        self.assertEqual(3, len(provider.contexts))
        history_request = provider.contexts[2]
        texts = []
        for message in history_request.messages:
            if message.role == "tool":
                texts.append("")
                continue
            parts = [
                block.text
                for block in message.content
                if isinstance(block, TextContent) and block.text
            ]
            texts.append("\n".join(parts))
        # user, assistant(tool intent, no text), tool result, final assistant, next user
        self.assertEqual(
            ["first user", "", "", "first answer", "second user"],
            texts,
        )
        self.assertTrue(any(message.role == "tool" for message in history_request.messages))

    @unittest.skip("OpenViking session recall multi-step reuse deferred to Task 12")
    async def test_session_recall_is_retrieved_once_and_reused_across_react_steps(self) -> None:
        return

    async def test_runner_applies_provider_timeout_seconds_to_generate(self) -> None:
        agent = Agent(
            agent_id="Pickle",
            workspace_path=Path("/tmp/pickle"),
            behavior_path=Path("/tmp/pickle/AGENT.md"),
            behavior_instruction="You are Pickle.",
            model_config=ModelConfig(
                provider="anthropic",
                model="claude-jupiter-v1-p",
                provider_options={"timeout_seconds": 0.01},
            ),
            tool_ids=[],
        )
        coordinator = AgentCoordinator(
            strategy=ReActStrategy(),
            deps=_deps(agent=agent, provider=HangingProvider()),
        )
        session = Session.create(agent_id="Pickle", session_id="session-1")

        with self.assertRaises(TimeoutError):
            await coordinator.run_turn(
                agent=agent,
                session=session,
                user_text="hello",
            )

        self.assertEqual(["user"], _entry_roles(session))

    def test_runner_uses_default_provider_timeout_when_not_configured(self) -> None:
        agent = Agent(
            agent_id="Pickle",
            workspace_path=Path("/tmp/pickle"),
            behavior_path=Path("/tmp/pickle/AGENT.md"),
            behavior_instruction="You are Pickle.",
            model_config=ModelConfig(
                provider="anthropic",
                model="claude-jupiter-v1-p",
                provider_options={},
            ),
            tool_ids=[],
        )
        deps = _deps(agent=agent, provider=StubProvider())

        self.assertEqual(
            600.0,
            ReActStrategy._provider_timeout_seconds(deps),
        )


if __name__ == "__main__":
    unittest.main()
