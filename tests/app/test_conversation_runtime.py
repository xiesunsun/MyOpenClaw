from __future__ import annotations

import asyncio
from types import SimpleNamespace

from pickel.app.conversation_runtime import ConversationRuntime
from pickel.app.runtime_models import TurnRequest
from pickel.conversations.agent_message import AssistantMessage, UserMessage
from pickel.conversations.content_blocks import TextContent
from pickel.conversations.conversation_service import ConversationService
from pickel.persistence.in_memory_runtime_store import InMemoryRuntimeStore
from pickel.runs.runtime_events import AssistantMessageEvent, TurnCompleted, TurnStarted


class _AgentRuntime:
    def __init__(self, conversation_service: ConversationService) -> None:
        self._conversation_service = conversation_service
        self.bindings = SimpleNamespace(
            tool_snapshot=SimpleNamespace(entries=()),
            tool_services=SimpleNamespace(skill_store=None),
        )

    async def accept_agent_run(self, *, session_id, user_message):
        self._conversation_service.append_user_message(
            session_id=session_id,
            message=user_message,
        )
        return SimpleNamespace(operation=SimpleNamespace(operation_id="operation-1"))

    async def drive_operation(self, operation_id, **_kwargs):
        message = AssistantMessage(content=[TextContent(text="done")])
        self._conversation_service.append_assistant_message(
            session_id="session-1",
            message=message,
        )
        return SimpleNamespace(
            operation_id=operation_id,
            status="succeeded",
            assistant_message=message,
        )


def test_conversation_runtime_drives_operation_and_projects_events() -> None:
    store = InMemoryRuntimeStore()
    service = ConversationService(
        store,
        session_id_factory=lambda: "session-1",
    )
    session = service.create_conversation_session(agent_id="Pickle", cwd="/project")
    loaded = SimpleNamespace(
        agent=SimpleNamespace(agent_id="Pickle"),
        version=SimpleNamespace(
            model=SimpleNamespace(
                provider="anthropic",
                model="claude-test",
                provider_options={},
            )
        ),
    )
    runtime = ConversationRuntime(
        loaded_agent_package=loaded,
        agent_runtime=_AgentRuntime(service),
        session=session,
        conversation_service=service,
        runtime_store=store,
        persistence="ephemeral",
        app_config=SimpleNamespace(),
    )
    events = []
    runtime.subscribe(events.append)

    result = asyncio.run(
        runtime.turn(
            TurnRequest(message=UserMessage(content=[TextContent(text="hello")]))
        )
    )

    assert result.status == "completed"
    assert result.turn_id == "operation-1"
    assert result.message is not None
    assert result.message.content[0].text == "done"
    assert [type(event) for event in events] == [
        TurnStarted,
        AssistantMessageEvent,
        TurnCompleted,
    ]
    assert all(event.envelope.turn_id == "operation-1" for event in events)
    assert runtime.snapshot().message_count == 2
