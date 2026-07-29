import asyncio
import unittest
from pathlib import Path

from pickel.agents.agent import Agent
from pickel.app.runtime import RuntimeConversation
from pickel.app.runtime_models import TurnInProgressError
from pickel.conversations.agent_message import AssistantMessage
from pickel.conversations.content_blocks import TextContent
from pickel.conversations.session import Session
from pickel.runs.runtime_events import AssistantMessageEvent
from pickel.shared.event_envelope import EventEnvelope
from pickel.shared.model_config import ModelConfig
from pickel.tools.base import FunctionTool
from pickel.tools.bus import ToolActivation, ToolBus, ToolSource


class _SessionService:
    def __init__(self) -> None:
        self.closed = False

    def close(self, *, session) -> None:
        self.closed = True

    def flush_new_entries(self, *, session, entries) -> None:
        pass


class _Run:
    def __init__(self, agent: Agent, *, wait: bool = False) -> None:
        self.agent = agent
        self.wait = wait
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.tool_bus = ToolBus()
        self.tool_bus.register(
            FunctionTool(
                name="read_file",
                description="test",
                input_schema={},
                func=lambda: "ok",
            ),
            source=ToolSource.BUILTIN,
        )
        self.activation = ToolActivation(allowed=frozenset({"read_file"}))
        self.skill_store = None

    async def turn(self, *, session, user_text, bus):
        self.started.set()
        if self.wait:
            await self.release.wait()
        reply = AssistantMessage(content=[TextContent(text=user_text)])
        await bus.emit(
            AssistantMessageEvent(
                envelope=EventEnvelope(session_id=session.session_id),
                text=user_text,
            )
        )
        return reply


def _agent() -> Agent:
    return Agent(
        agent_id="Pickle",
        workspace_path=Path("/tmp/pickel"),
        behavior_path=Path("/tmp/pickel/AGENT.md"),
        behavior_instruction="test",
        model_config=ModelConfig(provider="fake", model="model"),
        tool_ids=["read_file"],
    )


class RuntimeConversationTests(unittest.IsolatedAsyncioTestCase):
    async def test_turn_uses_owned_bus_and_assigns_monotonic_seq(self) -> None:
        agent = _agent()
        conversation = RuntimeConversation(
            agent=agent,
            run=_Run(agent),
            session=Session.create(agent_id=agent.agent_id),
        )
        events = []
        conversation.subscribe(events.append)

        await conversation.turn("one")
        await conversation.turn("two")

        self.assertEqual(["one", "two"], [item.text for item in events])
        self.assertEqual([0, 1], [item.envelope.seq for item in events])

    async def test_concurrent_turn_is_rejected(self) -> None:
        agent = _agent()
        run = _Run(agent, wait=True)
        conversation = RuntimeConversation(
            agent=agent,
            run=run,
            session=Session.create(agent_id=agent.agent_id),
        )
        first = asyncio.create_task(conversation.turn("one"))
        await run.started.wait()

        with self.assertRaises(TurnInProgressError):
            await conversation.turn("two")

        run.release.set()
        await first

    async def test_tools_are_filtered_by_current_activation(self) -> None:
        agent = _agent()
        run = _Run(agent)
        run.activation = ToolActivation(allowed=frozenset())
        conversation = RuntimeConversation(
            agent=agent,
            run=run,
            session=Session.create(agent_id=agent.agent_id),
        )

        self.assertEqual((), conversation.list_tools())

    async def test_archive_is_idempotent(self) -> None:
        agent = _agent()
        service = _SessionService()
        conversation = RuntimeConversation(
            agent=agent,
            run=_Run(agent),
            session=Session.create(agent_id=agent.agent_id),
            session_service=service,
        )

        conversation.archive()
        conversation.archive()

        self.assertTrue(service.closed)
        self.assertTrue(conversation.closed)
