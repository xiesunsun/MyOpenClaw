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
from pickel.conversations.agent_message import (
    AssistantMessage,
    ToolResultMessage,
    UserMessage,
)
from pickel.conversations.content_blocks import TextBlock, ToolCallBlock
from pickel.operations.agent_run_state import (
    AgentRunState,
    ModelStepState,
    ToolCallState,
)
from pickel.operations.operation_service import (
    OperationService,
    UnfinishedAgentRunError,
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
        runtime=AgentRuntimeSettings(
            max_model_steps=8,
            context_turn_window=5,
        ),
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
        runtime=provisional.runtime,
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
    assistant_message: AssistantMessage | None = None,
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
        appended_message=assistant_message
        or AssistantMessage(content=[TextBlock(text="working")]),
        appended_message_node_id=assistant_node_id,
    )
    return completed


def test_accept_agent_run_is_one_atomic_commit(store: OperationStore) -> None:
    package = _package()
    accepted = _service(store).accept_agent_run(
        session_id="session-1",
        agent_package_version_id=package.package_version_id,
        user_message=UserMessage(content=[TextBlock(text="hello")]),
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
    assert session.current_commit_sequence == 1
    assert accepted.operation.accepted_commit_sequence == 1
    assert accepted.operation.agent_package_version_id == package.package_version_id
    assert accepted.state.revision == 1
    assert accepted.state.status == "queued"
    assert accepted.user_message_entry.node.node_id == "user-node"
    assert conversation_reference is not None
    assert state_reference is not None
    assert (
        conversation_reference.commit_sequence == state_reference.commit_sequence == 1
    )


def test_accept_agent_run_rejects_missing_package_without_consuming_sequence(
    store: OperationStore,
) -> None:
    service = _service(store)

    with pytest.raises(StorageIntegrityError, match="AgentPackageVersion 不存在"):
        service.accept_agent_run(
            session_id="session-1",
            agent_package_version_id="missing",
            user_message=UserMessage(content=[TextBlock(text="hello")]),
        )

    session = store.load_conversation_session("session-1")
    assert session is not None
    assert session.current_commit_sequence == 0
    assert store.list_session_operations(session_id="session-1") == []


def test_second_accept_rejects_unfinished_run_without_appending_user_message(
    store: OperationStore,
) -> None:
    operation_ids = iter(("operation-1", "operation-2"))
    node_ids = iter(("user-node-1", "user-node-2"))
    service = OperationService(
        store,
        operation_id_factory=lambda: next(operation_ids),
        node_id_factory=lambda: next(node_ids),
    )
    service.accept_agent_run(
        session_id="session-1",
        agent_package_version_id=_package().package_version_id,
        user_message=UserMessage(content=[TextBlock(text="first")]),
    )

    with pytest.raises(
        UnfinishedAgentRunError,
        match=r"operation-1 \(queued\)",
    ):
        service.accept_agent_run(
            session_id="session-1",
            agent_package_version_id=_package().package_version_id,
            user_message=UserMessage(content=[TextBlock(text="must rollback")]),
        )

    session = store.load_conversation_session("session-1")
    entries = store.list_active_branch_entries(session_id="session-1")
    assert session is not None
    assert session.current_commit_sequence == 1
    assert len(entries) == 1
    assert entries[0].object.content["content"][0]["text"] == "first"


def test_cancel_closes_all_pending_tool_calls_and_allows_next_run(
    store: OperationStore,
) -> None:
    operation_ids = iter(("operation-1", "operation-2"))
    node_ids = iter(("user-node-1", "result-node-1", "result-node-2", "user-node-2"))
    service = OperationService(
        store,
        operation_id_factory=lambda: next(operation_ids),
        node_id_factory=lambda: next(node_ids),
    )
    accepted = service.accept_agent_run(
        session_id="session-1",
        agent_package_version_id=_package().package_version_id,
        user_message=UserMessage(content=[TextBlock(text="first")]),
    )
    model_completed = _commit_model_response(service, accepted=accepted)
    assert model_completed.current_step is not None
    tools_ready = AgentRunState(
        operation_id=model_completed.operation_id,
        revision=model_completed.revision + 1,
        status="running",
        user_message_node_id=model_completed.user_message_node_id,
        current_step=ModelStepState(
            step_id=model_completed.current_step.step_id,
            step_sequence=model_completed.current_step.step_sequence,
            phase="tool_calls_ready",
            assistant_message_node_id=(
                model_completed.current_step.assistant_message_node_id
            ),
            tool_calls=(
                ToolCallState(
                    tool_call_id="tool-1",
                    tool_name="echo",
                    arguments={"text": "first"},
                    execution_state="ready",
                ),
                ToolCallState(
                    tool_call_id="tool-2",
                    tool_name="echo",
                    arguments={"text": "second"},
                    execution_state="ready",
                ),
            ),
        ),
    )
    service.commit_agent_run_state(state=tools_ready)

    cancelled = service.cancel_agent_run(
        operation_id=accepted.operation.operation_id,
        reason="用户中断",
    )

    assert cancelled.status == "cancelled"
    assert cancelled.current_step is not None
    assert [call.execution_state for call in cancelled.current_step.tool_calls] == [
        "completed",
        "completed",
    ]
    entries = store.list_active_branch_entries(session_id="session-1")
    assert [entry.object.content["role"] for entry in entries] == [
        "user",
        "assistant",
        "tool",
        "tool",
    ]
    assert all(entry.object.content["is_error"] for entry in entries[2:])

    next_run = service.accept_agent_run(
        session_id="session-1",
        agent_package_version_id=_package().package_version_id,
        user_message=UserMessage(content=[TextBlock(text="continue")]),
    )
    assert next_run.operation.operation_id == "operation-2"


def test_failure_closes_tool_calls_even_before_prepare_phase_is_committed(
    store: OperationStore,
) -> None:
    node_ids = iter(("user-node", "result-node-1", "result-node-2"))
    service = OperationService(
        store,
        operation_id_factory=lambda: "operation-1",
        node_id_factory=lambda: next(node_ids),
    )
    accepted = service.accept_agent_run(
        session_id="session-1",
        agent_package_version_id=_package().package_version_id,
        user_message=UserMessage(content=[TextBlock(text="first")]),
    )
    _commit_model_response(
        service,
        accepted=accepted,
        assistant_message=AssistantMessage(
            content=[
                ToolCallBlock(
                    id="tool-1",
                    name="echo",
                    arguments={"text": "first"},
                ),
                ToolCallBlock(
                    id="tool-2",
                    name="echo",
                    arguments={"text": "second"},
                ),
            ]
        ),
    )

    failed = service.fail_agent_run(
        operation_id=accepted.operation.operation_id,
        error_type="RuntimeError",
        message="hook failed",
    )

    assert failed.status == "failed"
    assert failed.current_step is not None
    assert [call.execution_state for call in failed.current_step.tool_calls] == [
        "completed",
        "completed",
    ]
    entries = store.list_active_branch_entries(session_id="session-1")
    assert [entry.object.content["role"] for entry in entries] == [
        "user",
        "assistant",
        "tool",
        "tool",
    ]
    assert all(entry.object.content["is_error"] for entry in entries[2:])


def test_commit_message_and_state_share_commit_sequence(
    store: OperationStore,
) -> None:
    service = _service(store)
    accepted = service.accept_agent_run(
        session_id="session-1",
        agent_package_version_id=_package().package_version_id,
        user_message=UserMessage(content=[TextBlock(text="hello")]),
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
    assert (
        conversation_reference.commit_sequence == state_reference.commit_sequence == 4
    )


def test_recovery_preserves_unknown_tool_effect_without_replaying(
    store: OperationStore,
) -> None:
    service = _service(store)
    accepted = service.accept_agent_run(
        session_id="session-1",
        agent_package_version_id=_package().package_version_id,
        user_message=UserMessage(content=[TextBlock(text="hello")]),
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

    recovered_service = OperationService(store)
    recovered = recovered_service.list_unfinished_agent_runs(session_id="session-1")

    assert len(recovered) == 1
    assert recovered[0][1] == waiting
    assert (
        recovered[0][1].current_step.tool_calls[0].execution_state == "intent_recorded"
    )

    reconciled = recovered_service.record_reconciled_tool_result(
        operation_id="operation-1",
        result_message=ToolResultMessage(
            tool_call_id="tool-1",
            tool_name="external_action",
            content=[TextBlock(text="已由 Host 核实完成")],
        ),
    )

    assert reconciled.state.status == "running"
    assert reconciled.state.current_step is not None
    assert reconciled.state.current_step.tool_calls[0].execution_state == "completed"
    assert reconciled.appended_message_entry is not None


def test_state_reference_compare_and_swap_rejects_stale_version(
    store: OperationStore,
) -> None:
    service = _service(store)
    accepted = service.accept_agent_run(
        session_id="session-1",
        agent_package_version_id=_package().package_version_id,
        user_message=UserMessage(content=[TextBlock(text="hello")]),
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
        expected_commit_sequence=2,
    )
    object_id = stale.insert_immutable_object(
        object_type="session_operation_state",
        content={"operation_id": "operation-1"},
    )
    stale.move_named_reference(
        reference_name=operation_state_reference_name("operation-1"),
        target_kind="object",
        target_id=object_id,
        expected_current_commit_sequence=1,
    )

    with pytest.raises(StorageConflictError, match="commit_sequence 冲突"):
        stale.commit()

    session = store.load_conversation_session("session-1")
    assert session is not None
    assert session.current_commit_sequence == 2


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
        user_message=UserMessage(content=[TextBlock(text="hello")]),
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
