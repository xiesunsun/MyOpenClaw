from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

import pytest

from pickel.agents.agent_package import (
    AgentDefinition,
    AgentModelVersion,
    AgentPackageVersion,
    agent_package_digest,
)
from pickel.conversations.agent_message import AssistantMessage, UserMessage
from pickel.conversations.content_blocks import TextContent
from pickel.operations.agent_run_state import (
    AgentRunState,
    ModelStepState,
    ToolCallState,
)
from pickel.operations.operation_service import (
    OperationService,
    operation_state_reference_name,
)
from pickel.operations.operation_store import OperationStore
from pickel.persistence.in_memory_runtime_store import InMemoryRuntimeStore
from pickel.persistence.sqlite_runtime_store import SQLiteRuntimeStore
from pickel.persistence.storage_transaction import (
    StorageConflictError,
    StorageIntegrityError,
)


def _package() -> AgentPackageVersion:
    definition = AgentDefinition(
        agent_id="Pickle",
        workspace_path="/project",
        behavior_path="/project/AGENT.md",
        skills_path=None,
        tool_ids=("echo",),
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
        required_secrets=("api_key",),
    )
    provisional = AgentPackageVersion(
        package_version_id="pending",
        digest="pending",
        agent_id="Pickle",
        definition=definition,
        behavior_instruction="Be helpful.",
        model=model,
        skills=(),
        tools=(),
        created_at=datetime(2026, 8, 11, tzinfo=timezone.utc),
    )
    digest = agent_package_digest(provisional.content_dict())
    return AgentPackageVersion(
        package_version_id=f"agentpkg_{digest}",
        digest=digest,
        agent_id=provisional.agent_id,
        definition=definition,
        behavior_instruction=provisional.behavior_instruction,
        model=model,
        skills=(),
        tools=(),
        created_at=provisional.created_at,
    )


@pytest.fixture(params=["memory", "sqlite"])
def store(request, tmp_path: Path) -> OperationStore:
    factories: dict[str, Callable[[], OperationStore]] = {
        "memory": InMemoryRuntimeStore,
        "sqlite": lambda: SQLiteRuntimeStore(tmp_path / "runtime.db"),
    }
    value = factories[request.param]()
    value.create_conversation_session(
        session_id="session-1",
        agent_id="Pickle",
        cwd="/project",
    )
    value.insert_agent_package_version(_package())
    return value


def _service(store: OperationStore) -> OperationService:
    return OperationService(
        store,
        operation_id_factory=lambda: "operation-1",
        node_id_factory=lambda: "user-node",
    )


def _commit_model_response(
    service: OperationService,
    *,
    accepted,
    assistant_node_id: str = "assistant-node",
) -> AgentRunState:
    ready = AgentRunState(
        operation_id=accepted.operation.operation_id,
        revision=2,
        status="running",
        user_message_node_id=accepted.state.user_message_node_id,
        current_step=ModelStepState(
            step_id="step-1",
            step_sequence=1,
            phase="model_request_ready",
        ),
    )
    service.commit_agent_run_state(state=ready)
    intent_recorded = AgentRunState(
        operation_id=ready.operation_id,
        revision=3,
        status="running",
        user_message_node_id=ready.user_message_node_id,
        current_step=ModelStepState(
            step_id="step-1",
            step_sequence=1,
            phase="model_request_intent_recorded",
        ),
    )
    service.commit_agent_run_state(state=intent_recorded)
    completed = AgentRunState(
        operation_id=ready.operation_id,
        revision=4,
        status="running",
        user_message_node_id=ready.user_message_node_id,
        current_step=ModelStepState(
            step_id="step-1",
            step_sequence=1,
            phase="model_request_completed",
            assistant_message_node_id=assistant_node_id,
        ),
    )
    service.commit_agent_run_state(
        state=completed,
        appended_message=AssistantMessage(content=[TextContent(text="working")]),
        appended_message_node_id=assistant_node_id,
    )
    return completed


def test_accept_agent_run_is_one_atomic_commit(store: OperationStore) -> None:
    package = _package()
    accepted = _service(store).accept_agent_run(
        session_id="session-1",
        agent_package_version_id=package.package_version_id,
        user_message=UserMessage(content=[TextContent(text="hello")]),
    )

    session = store.load_conversation_session("session-1")
    conversation_reference = store.find_named_reference(
        session_id="session-1",
        reference_name="conversation/active",
    )
    state_reference = store.find_named_reference(
        session_id="session-1",
        reference_name=operation_state_reference_name("operation-1"),
    )

    assert session is not None
    assert session.current_sequence == 1
    assert accepted.operation.accepted_sequence == 1
    assert accepted.operation.agent_package_version_id == package.package_version_id
    assert accepted.state.revision == 1
    assert accepted.state.status == "queued"
    assert accepted.user_message_entry.node.node_id == "user-node"
    assert conversation_reference is not None
    assert state_reference is not None
    assert conversation_reference.sequence == state_reference.sequence == 1


def test_accept_agent_run_rejects_missing_package_without_consuming_sequence(
    store: OperationStore,
) -> None:
    service = _service(store)

    with pytest.raises(StorageIntegrityError, match="AgentPackageVersion 不存在"):
        service.accept_agent_run(
            session_id="session-1",
            agent_package_version_id="missing",
            user_message=UserMessage(content=[TextContent(text="hello")]),
        )

    session = store.load_conversation_session("session-1")
    assert session is not None
    assert session.current_sequence == 0
    assert store.list_session_operations(session_id="session-1") == []


def test_failed_second_accept_rolls_back_user_message_and_sequence(
    store: OperationStore,
) -> None:
    node_ids = iter(("user-node-1", "user-node-2"))
    service = OperationService(
        store,
        operation_id_factory=lambda: "operation-1",
        node_id_factory=lambda: next(node_ids),
    )
    service.accept_agent_run(
        session_id="session-1",
        agent_package_version_id=_package().package_version_id,
        user_message=UserMessage(content=[TextContent(text="first")]),
    )

    with pytest.raises(StorageIntegrityError, match="SessionOperation 已存在"):
        service.accept_agent_run(
            session_id="session-1",
            agent_package_version_id=_package().package_version_id,
            user_message=UserMessage(content=[TextContent(text="must rollback")]),
        )

    session = store.load_conversation_session("session-1")
    entries = store.list_active_branch_entries(session_id="session-1")
    assert session is not None
    assert session.current_sequence == 1
    assert len(entries) == 1
    assert entries[0].object.content["content"][0]["text"] == "first"


def test_commit_message_and_state_share_commit_sequence(
    store: OperationStore,
) -> None:
    service = _service(store)
    accepted = service.accept_agent_run(
        session_id="session-1",
        agent_package_version_id=_package().package_version_id,
        user_message=UserMessage(content=[TextContent(text="hello")]),
    )
    next_state = _commit_model_response(service, accepted=accepted)
    committed = service.load_agent_run_state(accepted.operation.operation_id)

    conversation_reference = store.find_named_reference(
        session_id="session-1",
        reference_name="conversation/active",
    )
    state_reference = store.find_named_reference(
        session_id="session-1",
        reference_name=operation_state_reference_name("operation-1"),
    )
    assert committed == next_state
    assert conversation_reference is not None
    assert state_reference is not None
    assert conversation_reference.sequence == state_reference.sequence == 4


def test_recovery_preserves_unknown_tool_effect_without_replaying(
    store: OperationStore,
) -> None:
    service = _service(store)
    accepted = service.accept_agent_run(
        session_id="session-1",
        agent_package_version_id=_package().package_version_id,
        user_message=UserMessage(content=[TextContent(text="hello")]),
    )
    _commit_model_response(service, accepted=accepted)
    tools_ready = AgentRunState(
        operation_id="operation-1",
        revision=5,
        status="running",
        user_message_node_id=accepted.state.user_message_node_id,
        current_step=ModelStepState(
            step_id="step-1",
            step_sequence=1,
            phase="tool_calls_ready",
            assistant_message_node_id="assistant-node",
            tool_calls=(
                ToolCallState(
                    tool_call_id="tool-1",
                    tool_name="external_action",
                    arguments={"target": "outside"},
                    execution_state="ready",
                ),
            ),
        ),
    )
    service.commit_agent_run_state(state=tools_ready)
    waiting = AgentRunState(
        operation_id="operation-1",
        revision=6,
        status="waiting",
        user_message_node_id=accepted.state.user_message_node_id,
        current_step=ModelStepState(
            step_id="step-1",
            step_sequence=1,
            phase="tool_calls_running",
            assistant_message_node_id="assistant-node",
            tool_calls=(
                ToolCallState(
                    tool_call_id="tool-1",
                    tool_name="external_action",
                    arguments={"target": "outside"},
                    execution_state="intent_recorded",
                ),
            ),
        ),
    )
    service.commit_agent_run_state(state=waiting)

    recovered = OperationService(store).list_unfinished_agent_runs(
        session_id="session-1"
    )

    assert len(recovered) == 1
    assert recovered[0][1] == waiting
    assert (
        recovered[0][1].current_step.tool_calls[0].execution_state == "intent_recorded"
    )


def test_state_reference_compare_and_swap_rejects_stale_version(
    store: OperationStore,
) -> None:
    service = _service(store)
    accepted = service.accept_agent_run(
        session_id="session-1",
        agent_package_version_id=_package().package_version_id,
        user_message=UserMessage(content=[TextContent(text="hello")]),
    )
    service.commit_agent_run_state(
        state=AgentRunState(
            operation_id="operation-1",
            revision=2,
            status="running",
            user_message_node_id=accepted.state.user_message_node_id,
        )
    )
    stale = store.begin_storage_transaction(
        session_id="session-1",
        expected_sequence=2,
    )
    object_id = stale.insert_immutable_object(
        object_type="session_operation_state",
        content={"operation_id": "operation-1"},
    )
    stale.move_named_reference(
        reference_name=operation_state_reference_name("operation-1"),
        target_kind="object",
        target_id=object_id,
        expected_current_sequence=1,
    )

    with pytest.raises(StorageConflictError, match="NamedReference sequence 冲突"):
        stale.commit()

    session = store.load_conversation_session("session-1")
    assert session is not None
    assert session.current_sequence == 2


def test_sqlite_store_recovers_latest_state_after_reopen(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime.db"
    store = SQLiteRuntimeStore(db_path)
    store.create_conversation_session(
        session_id="session-1",
        agent_id="Pickle",
        cwd="/project",
    )
    store.insert_agent_package_version(_package())
    service = _service(store)
    accepted = service.accept_agent_run(
        session_id="session-1",
        agent_package_version_id=_package().package_version_id,
        user_message=UserMessage(content=[TextContent(text="hello")]),
    )
    service.commit_agent_run_state(
        state=AgentRunState(
            operation_id="operation-1",
            revision=2,
            status="running",
            user_message_node_id=accepted.state.user_message_node_id,
        )
    )

    reopened = OperationService(SQLiteRuntimeStore(db_path))

    assert reopened.load_agent_run_state("operation-1").revision == 2
