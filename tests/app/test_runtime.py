import asyncio
import unittest
from pathlib import Path
from types import SimpleNamespace

from pickel.agents.agent import Agent
from pickel.app.runtime import RuntimeConversation, RuntimeHost
from pickel.app.runtime_models import TurnInProgressError
from pickel.conversations.agent_message import AssistantMessage
from pickel.conversations.content_blocks import TextContent
from pickel.conversations.session import Session
from pickel.runs.runtime_events import AssistantMessageEvent
from pickel.shared.event_envelope import EventEnvelope
from pickel.shared.model_config import ModelConfig
from pickel.tools.base import FunctionTool
from pickel.tools.bus import ToolActivation, ToolBus, ToolSource
from pickel.extensions_host.mcp_status import (
    McpServerStatusSnapshot,
    McpStatusSnapshot,
)
from pickel.extensions_host.registry import ExtensionRegistry


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


class RuntimeHostMcpInspectionTests(unittest.TestCase):
    def _conversation_with_one_active_mcp_tool(self) -> RuntimeConversation:
        agent = _agent()
        run = _Run(agent)
        run.tool_bus.register(
            FunctionTool(
                name="search",
                description="test",
                input_schema={},
                func=lambda: "ok",
            ),
            source=ToolSource.MCP,
            origin="github",
        )
        run.activation = ToolActivation(allowed=frozenset({"mcp__github__search"}))
        return RuntimeConversation(
            agent=agent,
            run=run,
            session=Session.create(agent_id=agent.agent_id),
        )

    def test_inspect_mcp_combines_server_state_with_conversation_activation(
        self,
    ) -> None:
        registry = ExtensionRegistry()
        registry.mcp_status_source = SimpleNamespace(
            snapshot=lambda: McpStatusSnapshot(
                servers=(
                    McpServerStatusSnapshot(
                        name="github",
                        status="connected",
                        config_scope="project",
                        protocol_version="2026-01-01",
                        implementation_name="github-mcp",
                        implementation_version="1.2.0",
                        discovered_tools=12,
                    ),
                ),
                diagnostics=("warning",),
            )
        )
        host = RuntimeHost(SimpleNamespace(extensions=registry, extension_result=None))

        inspection = host.inspect_mcp(self._conversation_with_one_active_mcp_tool())

        self.assertTrue(inspection.available)
        self.assertEqual(("warning",), inspection.diagnostics)
        self.assertEqual(12, inspection.servers[0].discovered_tools)
        self.assertEqual(1, inspection.servers[0].active_tools)
        self.assertEqual("github-mcp 1.2.0", inspection.servers[0].implementation)

    def test_inspect_mcp_distinguishes_unavailable_extension(self) -> None:
        host = RuntimeHost(
            SimpleNamespace(
                extensions=ExtensionRegistry(),
                extension_result=None,
            )
        )

        inspection = host.inspect_mcp(self._conversation_with_one_active_mcp_tool())

        self.assertFalse(inspection.available)
        self.assertEqual((), inspection.servers)
