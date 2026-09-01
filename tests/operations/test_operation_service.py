from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from pickel.agents.agent_package import (
    AgentPackageVersion,
    AgentRuntimePolicy,
    ImplementationRef,
    ModelPolicy,
    ModelVersion,
    WorkspacePolicy,
    build_agent_package_version,
)
from pickel.conversations.agent_message import UserMessage
from pickel.conversations.agent_message import AssistantMessage
from pickel.conversations.content_blocks import TextBlock
from pickel.conversations.conversation_node import ConversationNode
from pickel.inbox.message import UserMessageSource
from pickel.operations.agent_run_state import (
    AgentRunError,
    AgentRunState,
    ModelStepState,
)
from pickel.operations.operation_service import (
    OperationNotFoundError,
    OperationService,
)
from pickel.persistence.errors import StorageIntegrityError
from pickel.persistence.in_memory_runtime_store import InMemoryRuntimeStore
from pickel.persistence.sqlite_runtime_store import SQLiteRuntimeStore
from pickel.workspaces.workspace import Workspace
from pickel.workspaces.workspace_binding import WorkspaceBinding

NOW = datetime(2026, 8, 25, tzinfo=timezone.utc)


def _package(agent_id: str = "agent-1") -> AgentPackageVersion:
    return build_agent_package_version(
        agent_id=agent_id,
        format_version=1,
        behavior_instruction="test",
        model_policy=ModelPolicy(
            primary=ModelVersion(
                provider="test",
                model="test-model",
                wire_protocol="test",
                api_base=None,
                temperature=None,
                max_input_tokens=None,
                max_output_tokens=128,
                provider_options={},
                provider_implementation=ImplementationRef("provider", "test"),
                required_secret_refs=(),
            )
        ),
        runtime_policy=AgentRuntimePolicy(3, 10),
        workspace_policy=WorkspacePolicy("workspace"),
        skills=(),
        tools=(),
        extensions=(),
        created_at=NOW,
    )


@pytest.fixture(params=("memory", "sqlite"))
def setup_store(request: pytest.FixtureRequest, tmp_path: Path) -> Any:
    root = tmp_path / "workspace"
    root.mkdir()
    if request.param == "memory":
        store = InMemoryRuntimeStore()
    else:
        store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    store.create_session(
        workspace=Workspace("workspace-1", root, NOW),
        session=_session(root),
    )
    package = _package()
    store.insert_agent_package_version(package)
    message = store.send_message(
        message_id="message-1",
        session_id="session-1",
        delivery="followup",
        message=UserMessage(),
        source=UserMessageSource(),
        created_at=NOW,
    )
    return store, package, message, root


def _session(root: Path):
    from pickel.conversations.conversation_session import ConversationSession

    return ConversationSession(
        "session-1",
        "agent-1",
        "workspace-1",
        root,
        None,
        None,
        None,
        None,
        NOW,
        NOW,
        None,
    )


def _service(store: Any) -> OperationService:
    return OperationService(
        store,
        operation_id_factory=lambda: "operation-1",
        now=lambda: NOW,
    )


def _binding(root: Path, workspace_id: str = "workspace-1") -> WorkspaceBinding:
    return WorkspaceBinding(workspace_id, root, root)


def test_accept_pending_message_is_atomic(setup_store: Any) -> None:
    store, package, message, root = setup_store
    accepted = _service(store).accept_pending_message(
        message=message,
        agent_package_version_id=package.package_version_id,
        workspace_binding=_binding(root),
        expected_node_id=None,
    )

    assert accepted is not None
    assert accepted.operation.input_node_id == message.message_id
    assert accepted.state == AgentRunState(
        "operation-1", 1, "queued", None, 0, None, None, None, None
    )
    assert store.load_operation("operation-1") == accepted.operation
    assert store.load_run_state("operation-1") == accepted.state
    assert store.load_message(message.message_id).status == "claimed"
    assert store.load_session("session-1").active_operation_id == "operation-1"


def test_stale_active_leaf_cas_leaves_inbox_untouched(setup_store: Any) -> None:
    store, package, message, root = setup_store
    assert (
        _service(store).accept_pending_message(
            message=message,
            agent_package_version_id=package.package_version_id,
            workspace_binding=_binding(root),
            expected_node_id="stale-node",
        )
        is None
    )
    assert store.load_operation("operation-1") is None
    assert store.load_run_state("operation-1") is None
    assert store.load_message(message.message_id).status == "pending"


def test_duplicate_active_operation_is_cas_failure(setup_store: Any) -> None:
    store, package, message, root = setup_store
    service = _service(store)
    assert (
        service.accept_pending_message(
            message=message,
            agent_package_version_id=package.package_version_id,
            workspace_binding=_binding(root),
            expected_node_id=None,
        )
        is not None
    )
    second = store.send_message(
        message_id="message-2",
        session_id="session-1",
        delivery="followup",
        message=UserMessage(),
        source=UserMessageSource(),
        created_at=NOW,
    )
    assert (
        service.accept_pending_message(
            message=second,
            agent_package_version_id=package.package_version_id,
            workspace_binding=_binding(root),
            expected_node_id="message-1",
        )
        is None
    )


def test_claim_step_messages_validates_state_transition_before_store(
    setup_store: Any,
) -> None:
    store, package, message, root = setup_store
    service = _service(store)
    accepted = service.accept_pending_message(
        message=message,
        agent_package_version_id=package.package_version_id,
        workspace_binding=_binding(root),
        expected_node_id=None,
    )
    assert accepted is not None
    step = ModelStepState("step-1", 1, "preparing_request", 0, None, None, ())
    running = replace(accepted.state, revision=2, status="running", current_step=step)
    assert service.commit_transition(
        state=running,
        expected_revision=1,
        node=None,
        updated_at=NOW,
    )
    step_message = store.send_message(
        message_id="steer-1",
        session_id="session-1",
        delivery="steer",
        message=UserMessage(),
        source=UserMessageSource(),
        created_at=NOW,
    )

    invalid_step = replace(step, step_id="step-2")
    with pytest.raises(StorageIntegrityError):
        service.claim_step_messages(
            message_ids=(step_message.message_id,),
            state=replace(running, revision=3, current_step=invalid_step),
            expected_revision=2,
            updated_at=NOW,
        )
    with pytest.raises(StorageIntegrityError):
        service.claim_step_messages(
            message_ids=(step_message.message_id,),
            state=replace(running, revision=3, completed_step_count=2),
            expected_revision=2,
            updated_at=NOW,
        )
    assert not service.claim_step_messages(
        message_ids=(step_message.message_id,),
        state=replace(running, revision=3),
        expected_revision=1,
        updated_at=NOW,
    )
    assert store.load_run_state("operation-1") == running
    assert store.load_message(step_message.message_id).status == "pending"


@pytest.mark.parametrize(
    "package_id,binding",
    [
        ("missing", "workspace-1"),
        ("valid", "other-workspace"),
    ],
)
def test_accept_rejects_package_or_binding_mismatch(
    setup_store: Any, package_id: str, binding: str
) -> None:
    store, package, message, root = setup_store
    with pytest.raises(StorageIntegrityError):
        _service(store).accept_pending_message(
            message=message,
            agent_package_version_id=(
                package.package_version_id if package_id == "valid" else package_id
            ),
            workspace_binding=_binding(root, binding),
            expected_node_id=None,
        )
    assert store.load_operation("operation-1") is None
    assert store.load_message(message.message_id).status == "pending"


def test_load_list_and_revision_cas(setup_store: Any) -> None:
    store, package, message, root = setup_store
    service = _service(store)
    accepted = service.accept_pending_message(
        message=message,
        agent_package_version_id=package.package_version_id,
        workspace_binding=_binding(root),
        expected_node_id=None,
    )
    assert accepted is not None
    assert service.load_operation("operation-1") == accepted.operation
    assert service.list_operations(session_id="session-1") == (accepted.operation,)
    next_state = AgentRunState(
        "operation-1", 2, "running", None, 0, None, None, None, None
    )
    assert service.commit_state(state=next_state, expected_revision=1, updated_at=NOW)
    assert service.load_agent_run_state("operation-1") == next_state
    assert (
        service.commit_state(state=next_state, expected_revision=1, updated_at=NOW)
        is False
    )


def test_terminal_state_clears_active_operation(setup_store: Any) -> None:
    store, package, message, root = setup_store
    service = _service(store)
    accepted = service.accept_pending_message(
        message=message,
        agent_package_version_id=package.package_version_id,
        workspace_binding=_binding(root),
        expected_node_id=None,
    )
    assert accepted is not None
    failed = AgentRunState(
        "operation-1",
        2,
        "failed",
        None,
        0,
        None,
        None,
        AgentRunError("test", "failed", False),
        None,
    )
    assert service.commit_state(state=failed, expected_revision=1, updated_at=NOW)
    assert store.load_session("session-1").active_operation_id is None


def test_missing_operation_or_state_is_explicit(setup_store: Any) -> None:
    store, _package_value, _message, _root = setup_store
    service = _service(store)
    with pytest.raises(OperationNotFoundError):
        service.load_operation("missing")
    with pytest.raises(OperationNotFoundError):
        service.load_agent_run_state("missing")


def _running(service: OperationService) -> AgentRunState:
    running = AgentRunState(
        "operation-1", 2, "running", None, 0, None, None, None, None
    )
    assert service.commit_state(state=running, expected_revision=1, updated_at=NOW)
    return running


def _final_node(session_id: str = "session-1") -> ConversationNode:
    return ConversationNode(
        node_id="assistant-1",
        session_id=session_id,
        parent_node_id="message-1",
        content_type="agent_message",
        content=AssistantMessage(content=(TextBlock(text="done"),)),
        created_at=NOW,
    )


def test_commit_transition_atomically_appends_node_and_clears_active_operation(
    setup_store: Any,
) -> None:
    store, package, message, root = setup_store
    service = _service(store)
    assert (
        service.accept_pending_message(
            message=message,
            agent_package_version_id=package.package_version_id,
            workspace_binding=_binding(root),
            expected_node_id=None,
        )
        is not None
    )
    _running(service)
    terminal = AgentRunState(
        "operation-1", 3, "succeeded", None, 0, None, "assistant-1", None, None
    )

    assert service.commit_transition(
        state=terminal,
        expected_revision=2,
        node=_final_node(),
        updated_at=NOW,
    )
    assert store.load_node("assistant-1") is not None
    assert store.load_run_state("operation-1") == terminal
    assert store.load_session("session-1").active_operation_id is None


def test_commit_transition_cas_failure_leaves_node_and_state_untouched(
    setup_store: Any,
) -> None:
    store, package, message, root = setup_store
    service = _service(store)
    assert (
        service.accept_pending_message(
            message=message,
            agent_package_version_id=package.package_version_id,
            workspace_binding=_binding(root),
            expected_node_id=None,
        )
        is not None
    )
    running = _running(service)
    terminal = AgentRunState(
        "operation-1", 3, "succeeded", None, 0, None, "assistant-1", None, None
    )

    assert not service.commit_transition(
        state=terminal,
        expected_revision=1,
        node=_final_node(),
        updated_at=NOW,
    )
    assert store.load_node("assistant-1") is None
    assert store.load_run_state("operation-1") == running
    assert store.load_session("session-1").active_operation_id == "operation-1"


def test_commit_transition_invalid_node_leaves_node_and_state_untouched(
    setup_store: Any,
) -> None:
    store, package, message, root = setup_store
    service = _service(store)
    assert (
        service.accept_pending_message(
            message=message,
            agent_package_version_id=package.package_version_id,
            workspace_binding=_binding(root),
            expected_node_id=None,
        )
        is not None
    )
    running = _running(service)
    terminal = AgentRunState(
        "operation-1", 3, "succeeded", None, 0, None, "assistant-1", None, None
    )

    with pytest.raises(StorageIntegrityError):
        service.commit_transition(
            state=terminal,
            expected_revision=2,
            node=_final_node(session_id="other-session"),
            updated_at=NOW,
        )
    assert store.load_node("assistant-1") is None
    assert store.load_run_state("operation-1") == running
    assert store.load_session("session-1").active_operation_id == "operation-1"


def test_request_cancellation_is_persisted_and_idempotent(setup_store: Any) -> None:
    store, package, message, root = setup_store
    service = _service(store)
    assert service.accept_pending_message(
        message=message,
        agent_package_version_id=package.package_version_id,
        workspace_binding=_binding(root),
        expected_node_id=None,
    )
    _running(service)

    assert service.request_cancellation(
        "operation-1", reason="用户取消", requested_at=NOW
    )
    state = service.load_agent_run_state("operation-1")
    assert state.status == "cancelling"
    assert state.revision == 3
    assert state.cancellation is not None
    assert state.cancellation.cause == "用户取消"
    assert service.request_cancellation(
        "operation-1", reason="用户取消", requested_at=NOW
    )
    assert service.load_agent_run_state("operation-1") == state
    assert store.load_session("session-1").active_operation_id == "operation-1"
