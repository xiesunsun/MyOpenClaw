import asyncio
import unittest
from pathlib import Path

from pickel.agents.agent import Agent
from pickel.context.assembler import ContextAssembler
from pickel.context.model_context import ModelContext
from pickel.conversations.agent_message import AssistantMessage
from pickel.conversations.content_blocks import TextContent, ToolCallContent
from pickel.conversations.session import Session
from pickel.hooks.lifecycle import NoopLifecycleHooks
from pickel.providers.base import Provider
from pickel.runs import ReActStrategy, Run
from pickel.runs.event_bus import EventBus
from pickel.runs.runtime_events import (
    AssistantMessageEvent,
    StepStarted,
    ToolCallCompleted,
    ToolCallStarted,
)
from pickel.shared.model_config import ModelConfig
from pickel.tools.base import BaseTool, ToolExecutionContext, ToolExecutionResult, ToolSpec
from pickel.tools.shell import ShellSessionManager


def _assistant_text(message: AssistantMessage) -> str:
    return "\n".join(
        block.text for block in message.content if isinstance(block, TextContent) and block.text
    )


class StubProvider(Provider):
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


def _run(*, agent: Agent, provider: Provider, tools: list[BaseTool], strategy: ReActStrategy) -> Run:
    return Run(
        agent=agent,
        provider=provider,
        tools=tools,
        context_assembler=ContextAssembler(),
        lifecycle_hooks=NoopLifecycleHooks(),
        session_service=None,
        file_access_policy=None,
        workspace_files=None,
        shell_session_manager=ShellSessionManager(),
        unit_window=5,
        strategy=strategy,
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
        run = _run(
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
            strategy=ReActStrategy(max_steps=4),
        )
        session = Session.create(agent_id="Pickle", session_id="session-1")
        bus = EventBus()
        events = []
        bus.subscribe(lambda event: events.append(event))

        result = await run.turn(session=session, user_text="hello", bus=bus)

        self.assertEqual("done", _assistant_text(result))
        # 工具串行执行以保留 PreToolUse 控制点
        self.assertEqual(
            [
                StepStarted,
                ToolCallStarted,
                ToolCallCompleted,
                ToolCallStarted,
                ToolCallCompleted,
                StepStarted,
                AssistantMessageEvent,
            ],
            [type(event) for event in _without_turn_events(events)],
        )
        step_events = _without_turn_events(events)
        batch_id = step_events[1].batch_id
        self.assertTrue(batch_id)
        self.assertEqual(batch_id, step_events[2].batch_id)
        self.assertEqual(0, step_events[1].call_index)
        self.assertEqual("slow", step_events[2].tool_result.content)
        self.assertEqual(1, step_events[3].call_index)
        self.assertEqual("fast", step_events[4].tool_result.content)
        self.assertEqual("done", step_events[6].text)

    async def test_runner_emits_completed_event_with_is_error_for_failing_call(self) -> None:
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
        run = _run(
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
            strategy=ReActStrategy(max_steps=2),
        )
        session = Session.create(agent_id="Pickle", session_id="session-1")
        bus = EventBus()
        events = []
        bus.subscribe(lambda event: events.append(event))

        await run.turn(session=session, user_text="hello", bus=bus)

        failure = next(
            event
            for event in events
            if isinstance(event, ToolCallCompleted) and event.tool_result.is_error
        )
        self.assertEqual("missing", failure.tool_call.name)

    async def test_每个事件都带_session_id_turn_id_与递增_seq(self) -> None:
        """信封必须一路贯通到发射点，否则事件出不了进程。"""
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
        run = _run(
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
            strategy=ReActStrategy(max_steps=4),
        )
        session = Session.create(agent_id="Pickle", session_id="session-1")
        bus = EventBus()
        events = []
        bus.subscribe(lambda event: events.append(event))

        await run.turn(session=session, user_text="hello", bus=bus)

        self.assertTrue(events)
        self.assertEqual(list(range(len(events))), [e.envelope.seq for e in events])
        self.assertTrue(all(e.envelope.session_id == "session-1" for e in events))
        turn_ids = {e.envelope.turn_id for e in events}
        self.assertEqual(1, len(turn_ids))
        self.assertTrue(next(iter(turn_ids)))


def _without_turn_events(events):
    """滤掉 turn 级事件，只看 step 内序列。"""
    from pickel.runs.runtime_events import TurnCompleted, TurnStarted

    return [e for e in events if not isinstance(e, (TurnStarted, TurnCompleted))]


if __name__ == "__main__":
    unittest.main()
