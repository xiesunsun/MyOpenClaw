from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from pickel.context.model_context import ModelContext
from pickel.conversations.agent_message import AssistantMessage, UserMessage
from pickel.conversations.content_blocks import TextBlock
from pickel.conversations.conversation_service import ConversationService
from pickel.conversations.conversation_session import ConversationSession
from pickel.conversations.title_service import ConversationTitleService
from pickel.operations.session_operation import SessionOperation
from pickel.persistence.in_memory_runtime_store import InMemoryRuntimeStore
from pickel.persistence.sqlite_runtime_store import SQLiteRuntimeStore
from pickel.providers.prepared import PreparedModelCall
from pickel.providers.stream import StreamCompleted
from pickel.workspaces.workspace import Workspace
from pickel.workspaces.workspace_binding import WorkspaceBinding

NOW = datetime(2026, 8, 31, tzinfo=timezone.utc)


class _Provider:
    def __init__(self, output: str = "Generated title") -> None:
        self.output = output
        self.calls = 0

    def prepare(self, context: ModelContext) -> PreparedModelCall:
        return PreparedModelCall(
            provider="test",
            api_kind="test",
            endpoint="generate",
            requested_model="utility-model",
            body={"system": context.system.as_text()},
        )

    async def stream_prepared(self, prepared: PreparedModelCall):
        self.calls += 1
        yield StreamCompleted(
            AssistantMessage((TextBlock(self.output),)),
            provider_response={"id": "title"},
            http_status=200,
        )


def _store(request: pytest.FixtureRequest, tmp_path: Path):
    store = (
        InMemoryRuntimeStore()
        if request.param == "memory"
        else SQLiteRuntimeStore(tmp_path / "runtime.db")
    )
    store.create_session(
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
    return store


@pytest.fixture(params=("memory", "sqlite"))
def store(request: pytest.FixtureRequest, tmp_path: Path):
    return _store(request, tmp_path)


def _operation(store, text: str = "first user request") -> SessionOperation:
    node = ConversationService(store).append_user_message(
        session_id="session-1", message=UserMessage((TextBlock(text),))
    )
    return SessionOperation(
        operation_id="operation-1",
        session_id="session-1",
        agent_package_version_id="package-1",
        input_node_id=node.node_id,
        workspace_binding=WorkspaceBinding("workspace-1", Path.cwd(), None),
        accepted_at=NOW,
    )


def _package(provider: _Provider, *, utility: bool = True):
    version = SimpleNamespace(
        package_version_id="package-1",
        model_policy=SimpleNamespace(
            utility=(
                SimpleNamespace(provider="test", model="utility-model")
                if utility
                else None
            )
        ),
    )
    return SimpleNamespace(version=version, model_clients={"utility": provider})


def test_title_uses_utility_and_persists_observable_model_call(store) -> None:
    provider = _Provider()
    operation = _operation(store)
    result = asyncio.run(
        ConversationTitleService(store=store).generate(
            operation=operation, loaded_agent_package=_package(provider)
        )
    )

    assert result.committed is True
    assert result.used_fallback is False
    assert provider.calls == 1
    assert store.load_session("session-1").title == "Generated title"
    calls = store.list_model_calls(session_id="session-1")
    assert len(calls) == 1
    assert calls[0].model_role == "utility"
    assert calls[0].purpose == "title"
    assert calls[0].operation_id is None


def test_no_utility_falls_back_to_first_user_message(store) -> None:
    provider = _Provider()
    operation = _operation(store, "  a   deterministic   local title  ")
    result = asyncio.run(
        ConversationTitleService(store=store).generate(
            operation=operation, loaded_agent_package=_package(provider, utility=False)
        )
    )

    assert result.used_fallback is True
    assert result.committed is True
    assert result.title == "a deterministic local title"
    assert provider.calls == 0


def test_title_cas_and_user_title_win_under_concurrency(store) -> None:
    operation = _operation(store)
    package = _package(_Provider(), utility=False)
    service = ConversationTitleService(store=store)

    async def run_both():
        return await asyncio.gather(
            service.generate(operation=operation, loaded_agent_package=package),
            service.generate(operation=operation, loaded_agent_package=package),
        )

    results = asyncio.run(run_both())
    assert sum(result.committed for result in results) == 1

    store.set_user_title(session_id="session-1", title="User title", updated_at=NOW)
    later = asyncio.run(
        service.generate(operation=operation, loaded_agent_package=package)
    )
    assert later.committed is False
    assert store.load_session("session-1").title == "User title"
