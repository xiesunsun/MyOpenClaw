from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

import pytest

from pickel.agents.agent_package import (
    AgentDefinition,
    AgentModelVersion,
    AgentPackageVersion,
    AgentRuntimeSettings,
    agent_package_digest,
)
from pickel.conversations.agent_message import UserMessage
from pickel.conversations.content_blocks import TextContent
from pickel.operations.agent_run_state import AgentRunState, ModelStepState
from pickel.operations.operation_service import OperationService
from pickel.operations.operation_store import OperationStore
from pickel.persistence.in_memory_runtime_store import InMemoryRuntimeStore
from pickel.persistence.sqlite_runtime_store import SQLiteRuntimeStore
from pickel.persistence.storage_transaction import StorageIntegrityError


def _package() -> AgentPackageVersion:
    definition = AgentDefinition(
        agent_id="Pickle",
        workspace_path="/project",
        behavior_path="/project/AGENT.md",
        skills_path=None,
        tool_ids=(),
        extension_ids=(),
        file_access_mode="workspace",
        provider="anthropic",
        model="claude-test",
    )
    model = AgentModelVersion(
        provider="anthropic",
        model="claude-test",
        api_base=None,
        temperature=None,
        max_input_tokens=None,
        max_output_tokens=1024,
        provider_options={},
        required_secrets=(),
    )
    draft = AgentPackageVersion(
        package_version_id="pending",
        digest="pending",
        agent_id="Pickle",
        definition=definition,
        behavior_instruction="Be helpful.",
        model=model,
        runtime=AgentRuntimeSettings(
            max_model_steps=8,
            context_unit_window=5,
        ),
        skills=(),
        tools=(),
        created_at=datetime(2026, 8, 11, tzinfo=timezone.utc),
    )
    digest = agent_package_digest(draft.content_dict())
    return AgentPackageVersion(
        package_version_id=f"agentpkg_{digest}",
        digest=digest,
        agent_id=draft.agent_id,
        definition=definition,
        behavior_instruction=draft.behavior_instruction,
        model=model,
        runtime=draft.runtime,
        skills=(),
        tools=(),
        created_at=draft.created_at,
    )


@pytest.fixture(params=["memory", "sqlite"])
def store(request, tmp_path: Path) -> OperationStore:
    factories: dict[str, Callable[[], OperationStore]] = {
        "memory": InMemoryRuntimeStore,
        "sqlite": lambda: SQLiteRuntimeStore(tmp_path / "runtime.db"),
    }
    value = factories[request.param]()
    value.create_conversation_session(
        session_id="parent-session",
        agent_id="Pickle",
        cwd="/project",
    )
    value.create_conversation_session(
        session_id="child-session",
        agent_id="Pickle",
        cwd="/project",
    )
    value.insert_agent_package_version(_package())
    return value


def _service(store: OperationStore) -> OperationService:
    operation_ids = iter(("parent-operation", "child-operation"))
    node_ids = iter(("parent-user-node", "child-user-node"))
    return OperationService(
        store,
        operation_id_factory=lambda: next(operation_ids),
        delegation_id_factory=lambda: "delegation-1",
        node_id_factory=lambda: next(node_ids),
    )


def _start_parent_step(service: OperationService) -> None:
    accepted = service.accept_agent_run(
        session_id="parent-session",
        agent_package_version_id=_package().package_version_id,
        user_message=UserMessage(content=[TextContent(text="parent")]),
    )
    service.commit_agent_run_state(
        state=AgentRunState(
            operation_id=accepted.operation.operation_id,
            revision=2,
            status="running",
            user_message_node_id=accepted.state.user_message_node_id,
            current_step=ModelStepState(
                step_id="parent-step",
                step_sequence=1,
                phase="model_request_ready",
            ),
        )
    )


def test_delegation_is_atomic_with_child_operation_acceptance(
    store: OperationStore,
) -> None:
    service = _service(store)
    _start_parent_step(service)

    result = service.accept_delegated_agent_run(
        session_id="child-session",
        agent_package_version_id=_package().package_version_id,
        user_message=UserMessage(content=[TextContent(text="delegated work")]),
        parent_operation_id="parent-operation",
        parent_step_id="parent-step",
    )

    assert result.accepted_run.operation.operation_id == "child-operation"
    assert result.delegation.parent_operation_id == "parent-operation"
    assert result.delegation.child_operation_id == "child-operation"
    assert result.delegation.child_session_id == "child-session"
    assert result.delegation.created_commit_sequence == 1
    assert (
        result.delegation.created_commit_sequence
        == result.accepted_run.operation.accepted_commit_sequence
    )
    assert service.list_agent_delegations(parent_operation_id="parent-operation") == [
        result.delegation
    ]
    assert (
        store.find_delegation_by_child_operation("child-operation") == result.delegation
    )


def test_invalid_parent_step_does_not_accept_child_operation(
    store: OperationStore,
) -> None:
    service = _service(store)
    _start_parent_step(service)

    with pytest.raises(StorageIntegrityError, match="当前 ModelStep"):
        service.accept_delegated_agent_run(
            session_id="child-session",
            agent_package_version_id=_package().package_version_id,
            user_message=UserMessage(content=[TextContent(text="must rollback")]),
            parent_operation_id="parent-operation",
            parent_step_id="stale-step",
        )

    child_session = store.load_conversation_session("child-session")
    assert child_session is not None
    assert child_session.current_commit_sequence == 0
    assert store.list_session_operations(session_id="child-session") == []


def test_start_delegated_run_creates_isolated_child_session(
    store: OperationStore,
) -> None:
    operation_ids = iter(("parent-operation", "dynamic-child-operation"))
    node_ids = iter(("parent-user-node", "dynamic-child-user-node"))
    service = OperationService(
        store,
        operation_id_factory=lambda: next(operation_ids),
        delegation_id_factory=lambda: "dynamic-delegation",
        session_id_factory=lambda: "dynamic-child-session",
        node_id_factory=lambda: next(node_ids),
    )
    _start_parent_step(service)

    result = service.start_delegated_run(
        agent_package_version_id=_package().package_version_id,
        user_message=UserMessage(content=[TextContent(text="isolated work")]),
        parent_operation_id="parent-operation",
        parent_step_id="parent-step",
    )

    child_session = store.load_conversation_session("dynamic-child-session")
    assert child_session is not None
    assert child_session.session_id != "parent-session"
    assert result.delegation.child_session_id == child_session.session_id
    assert result.accepted_run.operation.session_id == child_session.session_id
