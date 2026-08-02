import asyncio
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from pickel.agents.agent import Agent
from pickel.app.runtime import RuntimeConversation, RuntimeHost
from pickel.app.runtime_models import (
    ConversationRequest,
    TurnInProgressError,
    TurnRequest,
)
from pickel.conversations.agent_message import AssistantMessage, UserMessage
from pickel.conversations.content_blocks import TextContent
from pickel.conversations.session import Session
from pickel.runs.runtime_events import AssistantMessageEvent
from pickel.shared.conversation_output import AudioContent, AudioOutputReady
from pickel.shared.event_envelope import EventEnvelope
from pickel.shared.model_config import ModelConfig
from pickel.tools.base import FunctionTool
from pickel.tools.bus import ToolActivation, ToolBus, ToolSource
from pickel.extensions_host.mcp_status import (
    McpServerStatusSnapshot,
    McpStatusSnapshot,
)
from pickel.extensions_host.registry import ExtensionRegistry
from pickel.extensions_host.host import ExtensionHost


class _SessionService:
    def __init__(self) -> None:
        self.closed = False

    def close(self, *, session) -> None:
        self.closed = True

    def flush_new_entries(self, *, session, entries) -> None:
        pass


class _BashOperations:
    def __init__(self) -> None:
        self.closed_sessions: list[str] = []

    def close(self, session_id: str) -> None:
        self.closed_sessions.append(session_id)


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
        self.bash_operations = _BashOperations()

    async def turn(self, *, session, user_message, bus):
        self.started.set()
        if self.wait:
            await self.release.wait()
        user_text = user_message.content[0].text
        reply = AssistantMessage(content=[TextContent(text=user_text)])
        await bus.emit(
            AssistantMessageEvent(
                envelope=EventEnvelope(session_id=session.session_id),
                text=user_text,
            )
        )
        return reply


class _EventProcessor:
    def __init__(self) -> None:
        self.events = []
        self.closed = False

    async def handle_event(self, event) -> None:
        self.events.append(event)

    def close(self) -> None:
        self.closed = True


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
    @staticmethod
    def _request(text: str) -> TurnRequest:
        return TurnRequest(message=UserMessage(content=[TextContent(text=text)]))

    async def test_turn_uses_owned_bus_and_assigns_monotonic_seq(self) -> None:
        agent = _agent()
        conversation = RuntimeConversation(
            agent=agent,
            run=_Run(agent),
            session=Session.create(agent_id=agent.agent_id),
        )
        events = []
        conversation.subscribe(events.append)

        first = await conversation.turn(self._request("one"))
        second = await conversation.turn(self._request("two"))

        self.assertEqual("completed", first.status)
        self.assertEqual("completed", second.status)
        self.assertEqual(["one", "two"], [item.text for item in events])
        self.assertEqual([0, 1], [item.envelope.seq for item in events])

    async def test_publishes_outputs_to_surface_subscriber(self) -> None:
        agent = _agent()
        conversation = RuntimeConversation(
            agent=agent,
            run=_Run(agent),
            session=Session.create(agent_id=agent.agent_id),
        )
        outputs = []
        conversation.subscribe_outputs(outputs.append)
        output = AudioOutputReady(
            session_id=conversation.session.session_id,
            turn_id="turn-1",
            source="test",
            audio=AudioContent(data=b"wav", media_type="audio/wav"),
        )

        await conversation.publish_output(output)

        self.assertEqual([output], outputs)

    async def test_detach_cancels_owned_background_tasks(self) -> None:
        agent = _agent()
        conversation = RuntimeConversation(
            agent=agent,
            run=_Run(agent),
            session=Session.create(agent_id=agent.agent_id),
        )
        started = asyncio.Event()
        stopped = asyncio.Event()

        async def worker() -> None:
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

        conversation.start_background_task(worker(), "test-worker")
        await started.wait()

        conversation.detach()
        await asyncio.wait_for(stopped.wait(), timeout=1)

        self.assertTrue(conversation.closed)

    async def test_concurrent_turn_is_rejected(self) -> None:
        agent = _agent()
        run = _Run(agent, wait=True)
        conversation = RuntimeConversation(
            agent=agent,
            run=run,
            session=Session.create(agent_id=agent.agent_id),
        )
        first = asyncio.create_task(conversation.turn(self._request("one")))
        await run.started.wait()

        with self.assertRaises(TurnInProgressError):
            await conversation.turn(self._request("two"))

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
        run = _Run(agent)
        session = Session.create(agent_id=agent.agent_id)
        conversation = RuntimeConversation(
            agent=agent,
            run=run,
            session=session,
            session_service=service,
        )

        conversation.archive()
        conversation.archive()

        self.assertTrue(service.closed)
        self.assertTrue(conversation.closed)
        self.assertEqual([session.session_id], run.bash_operations.closed_sessions)


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


class RuntimeHostConversationTests(unittest.TestCase):
    def test_ephemeral_conversation_does_not_build_session_service(self) -> None:
        agent = _agent()
        run = _Run(agent)
        boot = SimpleNamespace(
            app_config=SimpleNamespace(observability=SimpleNamespace(trace=None)),
            extensions=ExtensionRegistry(),
            extension_result=None,
            build_run=lambda agent_id=None: (agent, run),
            build_session_service=Mock(
                side_effect=AssertionError("不应创建持久化服务")
            ),
        )

        conversation = RuntimeHost(boot).open_conversation(
            ConversationRequest(
                agent_id=agent.agent_id,
                persistence="ephemeral",
                cwd=Path("/tmp/pickel"),
            )
        )

        self.assertIsNone(conversation.session_service)
        self.assertEqual(str(Path("/tmp/pickel").resolve()), conversation.session.cwd)

    def test_resume_rejects_conflicting_agent(self) -> None:
        with self.assertRaisesRegex(ValueError, "不能同时指定 agent_id"):
            ConversationRequest(agent_id="A", session_id="session-1")

    def test_attach_resolves_and_closes_interactive_event_processor(self) -> None:
        agent = _agent()
        run = _Run(agent)
        registry = ExtensionRegistry()
        processor = _EventProcessor()
        ExtensionHost(
            name="demo",
            config_section=None,
            tool_bus=ToolBus(),
            registry=registry,
        ).add_event_processor(
            event_types=(AssistantMessageEvent,),
            factory=lambda context: (
                processor if context.mode == "interactive" else None
            ),
        )
        boot = SimpleNamespace(
            app_config=SimpleNamespace(observability=SimpleNamespace(trace=None)),
            extensions=registry,
            extension_result=None,
        )

        conversation = RuntimeHost(boot).attach(
            agent=agent,
            run=run,
            session=Session.create(agent_id=agent.agent_id),
            mode="interactive",
        )
        conversation.archive()

        self.assertTrue(processor.closed)
