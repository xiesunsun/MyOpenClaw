from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

import pytest

from pickel.context.model_context import ModelContext, SystemContent
from pickel.conversations.agent_message import AssistantMessage
from pickel.conversations.content_blocks import TextBlock
from pickel.conversations.conversation_session import ConversationSession
from pickel.model_calls.model_call import ModelCallPurpose, ModelRole
from pickel.model_calls.prepared import PreparedModelCall
from pickel.model_calls.service import ModelCallService
from pickel.persistence.in_memory_runtime_store import InMemoryRuntimeStore
from pickel.persistence.sqlite_runtime_store import SQLiteRuntimeStore
from pickel.providers.stream import StreamCompleted
from pickel.runtime.model_call_send_gate import ModelCallSendGate
from pickel.runtime.runtime_effects import RuntimeEffects
from pickel.workspaces.workspace import Workspace

NOW = datetime(2026, 8, 27, tzinfo=timezone.utc)


class _Mapper:
    def prepare(self, context: ModelContext) -> PreparedModelCall:
        return PreparedModelCall(
            provider="test",
            api_kind="test",
            endpoint="generate",
            requested_model="test-model",
            body={"system": context.system.as_text(), "stream": True},
        )


class _Provider:
    def __init__(self) -> None:
        self.bodies = []

    async def stream_prepared(self, prepared: PreparedModelCall):
        self.bodies.append(prepared.body)
        yield StreamCompleted(
            AssistantMessage((TextBlock("utility output"),)),
            provider_response={"id": "response-utility"},
            http_status=200,
        )


@pytest.fixture(params=("memory", "sqlite"))
def store(request: pytest.FixtureRequest, tmp_path: Path):
    if request.param == "memory":
        result = InMemoryRuntimeStore()
    else:
        result = SQLiteRuntimeStore(tmp_path / "runtime.db")
    result.create_session(
        workspace=Workspace("workspace-1", tmp_path, NOW),
        session=ConversationSession(
            session_id="session-1",
            agent_id="Pickle",
            workspace_id="workspace-1",
            cwd=tmp_path,
            active_node_id=None,
            active_operation_id=None,
            title=None,
            title_source=None,
            created_at=NOW,
            updated_at=NOW,
            archived_at=None,
        ),
    )
    return result


@pytest.mark.parametrize(
    ("model_role", "purpose"),
    (("utility", "title"), ("worker", "history_compaction")),
)
def test_utility_and_worker_calls_use_the_same_prepared_contract(
    store,
    model_role: ModelRole,
    purpose: ModelCallPurpose,
) -> None:
    context = ModelContext(SystemContent.from_text("context"), (), ())
    service = ModelCallService(store)
    prepared_call = service.prepare_session_call(
        session_id="session-1",
        context=context,
        mapper=_Mapper(),
        request_attempt=1,
        model_role=model_role,
        purpose=purpose,
    )
    provider = _Provider()
    response = asyncio.run(
        ModelCallSendGate(store).send(
            call=prepared_call.model_call,
            prepared=prepared_call.prepared,
            effects=RuntimeEffects(provider=provider),
        )
    )
    completed = service.complete_session_response(
        call=prepared_call.model_call,
        response=response,
    )

    assert completed.status == "completed"
    assert completed.operation_id is None
    assert completed.purpose == purpose
    assert completed.model_role == model_role
    assert provider.bodies == [prepared_call.prepared.body]
    assert (
        service.request_content(completed).wire_request == prepared_call.prepared.body
    )
