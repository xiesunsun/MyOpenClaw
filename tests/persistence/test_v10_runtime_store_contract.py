"""SQLite/InMemory v10 Runtime Store 的共享行为合同。

这些测试只依赖公开 Store 方法；同一套断言必须同时适用于两个适配器。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from typing import Any, Callable

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
from pickel.artifacts.artifact import Artifact, ArtifactReference
from pickel.conversations.agent_message import AssistantMessage, UserMessage
from pickel.conversations.content_blocks import ArtifactBlock, TextBlock
from pickel.conversations.conversation_node import ConversationNode
from pickel.conversations.conversation_session import ConversationSession
from pickel.context.model_context import ModelContext, SystemContent
from pickel.inbox.message import (
    AgentSettledMessageSource,
    InboxMessage,
    UserMessageSource,
    agent_settled_message_id,
)
from pickel.operations.agent_delegation import AgentDelegation
from pickel.operations.agent_run_state import (
    AgentRunError,
    AgentRunState,
    Cancellation,
    ModelRequestIntent,
    ModelStepState,
)
from pickel.operations.session_operation import SessionOperation
from pickel.persistence.errors import StorageConflictError, StorageIntegrityError
from pickel.persistence.in_memory_runtime_store import InMemoryRuntimeStore
from pickel.persistence.sqlite_runtime_store import SQLiteRuntimeStore
from pickel.workspaces.workspace import Workspace
from pickel.workspaces.workspace_binding import WorkspaceBinding

UTC = timezone.utc
NOW = datetime(2026, 8, 25, 0, 0, tzinfo=UTC)
Store = Any
StoreFactory = Callable[[Path], Store]


@pytest.fixture(params=("memory", "sqlite"))
def store_factory(request: pytest.FixtureRequest) -> StoreFactory:
    if request.param == "memory":
        return lambda _root: InMemoryRuntimeStore()
    return lambda root: SQLiteRuntimeStore(root / "runtime.db")


def _session(
    session_id: str,
    workspace_id: str,
    root: Path,
    *,
    agent_id: str = "agent-1",
) -> ConversationSession:
    return ConversationSession(
        session_id=session_id,
        agent_id=agent_id,
        workspace_id=workspace_id,
        cwd=root,
        active_node_id=None,
        active_operation_id=None,
        title=None,
        title_source=None,
        created_at=NOW,
        updated_at=NOW,
        archived_at=None,
    )


def _create_session(
    store: Store,
    session_id: str,
    workspace_id: str,
    root: Path,
    *,
    agent_id: str = "agent-1",
) -> None:
    root.mkdir(parents=True, exist_ok=True)
    store.create_session(
        workspace=Workspace(workspace_id, root, NOW),
        session=_session(session_id, workspace_id, root, agent_id=agent_id),
    )


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


def _send(
    store: Store,
    session_id: str,
    message_id: str,
    *,
    message: UserMessage | None = None,
    delivery: str = "followup",
) -> InboxMessage:
    return store.send_message(
        message_id=message_id,
        session_id=session_id,
        delivery=delivery,
        message=message or UserMessage((TextBlock(message_id),)),
        source=UserMessageSource(),
        created_at=NOW,
    )


def _operation(
    operation_id: str,
    session_id: str,
    package: AgentPackageVersion,
    input_node_id: str,
    workspace_id: str,
    root: Path,
) -> SessionOperation:
    return SessionOperation(
        operation_id=operation_id,
        session_id=session_id,
        agent_package_version_id=package.package_version_id,
        workspace_binding=WorkspaceBinding(workspace_id, root, None),
        input_node_id=input_node_id,
        accepted_at=NOW,
    )


def _queued(operation_id: str) -> AgentRunState:
    return AgentRunState(
        operation_id=operation_id,
        revision=1,
        status="queued",
        waiting_reason=None,
        completed_step_count=0,
        current_step=None,
        final_assistant_node_id=None,
        error=None,
        cancellation=None,
    )


def _accept_one(
    store: Store,
    root: Path,
    *,
    session_id: str = "session-1",
    workspace_id: str = "workspace-1",
    operation_id: str = "operation-1",
    message_id: str = "message-1",
) -> tuple[AgentPackageVersion, SessionOperation, AgentRunState]:
    package = _package()
    store.insert_agent_package_version(package)
    _send(store, session_id, message_id)
    operation = _operation(
        operation_id,
        session_id,
        package,
        message_id,
        workspace_id,
        root,
    )
    state = _queued(operation_id)
    assert store.accept_operation(
        operation=operation,
        state=state,
        expected_node_id=None,
    )
    return package, operation, state


def test_create_session_is_atomic_when_session_insert_fails(
    store_factory: StoreFactory, tmp_path: Path
) -> None:
    store = store_factory(tmp_path)
    _create_session(store, "session-1", "workspace-1", tmp_path / "one")

    # 新 Workspace 必须和失败的 Session 插入一起回滚。
    with pytest.raises(StorageIntegrityError):
        _create_session(store, "session-1", "workspace-2", tmp_path / "two")
    assert store.load_workspace("workspace-2") is None
    assert store.load_session("session-1").workspace_id == "workspace-1"


def test_inbox_sequence_archive_and_archive_idempotence(
    store_factory: StoreFactory, tmp_path: Path
) -> None:
    store = store_factory(tmp_path)
    _create_session(store, "session-1", "workspace-1", tmp_path / "one")
    first = _send(store, "session-1", "message-1")
    second = _send(store, "session-1", "message-2", delivery="steer")
    assert (first.sequence, second.sequence) == (1, 2)

    assert store.discard_message(message_id="message-1", reason="test", handled_at=NOW)
    assert store.discard_message(message_id="message-2", reason="test", handled_at=NOW)
    archived_at = NOW + timedelta(minutes=1)
    store.archive_session(session_id="session-1", archived_at=archived_at)
    store.archive_session(session_id="session-1", archived_at=archived_at)
    assert store.load_session("session-1").archived_at == archived_at
    with pytest.raises(StorageIntegrityError):
        _send(store, "session-1", "message-3")


def test_active_leaf_uses_cas_and_rejects_cross_session_reference(
    store_factory: StoreFactory, tmp_path: Path
) -> None:
    store = store_factory(tmp_path)
    _create_session(store, "session-1", "workspace-1", tmp_path / "one")
    _create_session(store, "session-2", "workspace-2", tmp_path / "two")
    node = ConversationNode(
        "node-1",
        "session-1",
        None,
        "agent_message",
        UserMessage((TextBlock("hello"),)),
        NOW,
    )
    assert store.append_node(node=node, expected_node_id=None)
    assert not store.move_active_node(
        session_id="session-1",
        expected_node_id="stale-node",
        new_node_id=None,
        updated_at=NOW,
    )
    with pytest.raises(StorageIntegrityError):
        store.move_active_node(
            session_id="session-2",
            expected_node_id=None,
            new_node_id="node-1",
            updated_at=NOW,
        )


def test_accept_operation_commits_all_facts_or_leaves_no_residue(
    store_factory: StoreFactory, tmp_path: Path
) -> None:
    store = store_factory(tmp_path)
    _create_session(store, "session-1", "workspace-1", tmp_path / "one")
    package = _package()
    store.insert_agent_package_version(package)
    _send(store, "session-1", "message-1")
    operation = _operation(
        "operation-1", "session-1", package, "message-1", "workspace-1", tmp_path
    )
    state = _queued("operation-1")
    assert not store.accept_operation(
        operation=operation,
        state=state,
        expected_node_id="stale-leaf",
    )
    assert store.load_node("message-1") is None
    assert store.load_operation("operation-1") is None
    assert store.load_run_state("operation-1") is None
    assert store.load_message("message-1").status == "pending"
    assert store.load_session("session-1").active_operation_id is None

    assert store.accept_operation(
        operation=operation,
        state=state,
        expected_node_id=None,
    )
    assert store.load_node("message-1") is not None
    assert store.load_operation("operation-1") == operation
    assert store.load_run_state("operation-1") == state
    assert store.load_message("message-1").status == "claimed"
    assert store.load_session("session-1").active_operation_id == "operation-1"


def test_run_state_revision_is_exactly_one_and_expired_cas_is_noop(
    store_factory: StoreFactory, tmp_path: Path
) -> None:
    store = store_factory(tmp_path)
    _create_session(store, "session-1", "workspace-1", tmp_path / "one")
    _, _, initial = _accept_one(store, tmp_path)
    running = replace(initial, revision=2, status="running")
    assert store.commit_run_transition(
        state=running,
        expected_revision=1,
        updated_at=NOW + timedelta(seconds=1),
        node=None,
    )
    with pytest.raises(StorageIntegrityError):
        store.commit_run_transition(
            state=replace(running, revision=4),
            expected_revision=2,
            updated_at=NOW,
            node=None,
        )
    assert not store.commit_run_transition(
        state=replace(running, revision=2),
        expected_revision=1,
        updated_at=NOW,
        node=None,
    )
    assert store.load_run_state("operation-1") == running


def _prepare_step(
    store: Store, root: Path
) -> tuple[AgentRunState, AgentRunState, ModelStepState]:
    _, _, initial = _accept_one(store, root)
    step = ModelStepState("step-1", 1, "preparing_request", 0, None, None, ())
    running = replace(initial, revision=2, status="running", current_step=step)
    assert store.commit_run_transition(
        state=running,
        expected_revision=1,
        updated_at=NOW,
        node=None,
    )
    return initial, running, step


def test_claim_step_messages_batches_fifo_nodes_and_excludes_followup(
    store_factory: StoreFactory, tmp_path: Path
) -> None:
    store = store_factory(tmp_path)
    _create_session(store, "session-1", "workspace-1", tmp_path / "one")
    _, running, step = _prepare_step(store, tmp_path)
    _send(store, "session-1", "steer-1", delivery="steer")
    _send(store, "session-1", "followup-1", delivery="followup")
    _send(store, "session-1", "inject-1", delivery="inject")

    assert [
        message.message_id
        for message in store.list_pending_step_messages(session_id="session-1")
    ] == ["steer-1", "inject-1"]
    next_state = replace(running, revision=3)
    assert store.claim_step_messages(
        message_ids=("steer-1", "inject-1"),
        state=next_state,
        expected_revision=2,
        updated_at=NOW,
    )

    first = store.load_node("steer-1")
    second = store.load_node("inject-1")
    assert first is not None and second is not None
    assert first.parent_node_id == "message-1"
    assert second.parent_node_id == "steer-1"
    assert store.load_session("session-1").active_node_id == "inject-1"
    assert store.load_message("steer-1").claimed_operation_id == "operation-1"
    assert store.load_message("steer-1").claimed_step_id == step.step_id
    assert store.load_message("inject-1").claimed_step_id == step.step_id
    assert store.load_message("followup-1").status == "pending"
    assert store.load_run_state("operation-1") == next_state


def test_claim_step_messages_rejects_expired_cross_session_and_partial_batches(
    store_factory: StoreFactory, tmp_path: Path
) -> None:
    store = store_factory(tmp_path)
    _create_session(store, "session-1", "workspace-1", tmp_path / "one")
    _create_session(store, "session-2", "workspace-2", tmp_path / "two")
    _, running, _ = _prepare_step(store, tmp_path)
    _send(store, "session-1", "steer-1", delivery="steer")
    _send(store, "session-1", "inject-1", delivery="inject")
    _send(store, "session-2", "steer-2", delivery="steer")

    next_state = replace(running, revision=3)
    for message_ids, expected_revision in (
        (("inject-1", "steer-1"), 2),
        (("steer-1", "steer-1"), 2),
        (("steer-1", "missing"), 2),
        (("steer-2",), 2),
        (("steer-1", "inject-1"), 1),
    ):
        assert not store.claim_step_messages(
            message_ids=message_ids,
            state=next_state,
            expected_revision=expected_revision,
            updated_at=NOW,
        )
    assert store.load_node("steer-1") is None
    assert store.load_node("inject-1") is None
    assert store.load_message("steer-1").status == "pending"
    assert store.load_message("inject-1").status == "pending"
    assert store.load_session("session-1").active_node_id == "message-1"
    assert store.load_run_state("operation-1") == running


def test_commit_request_intent_and_success_stop_when_step_messages_are_pending(
    store_factory: StoreFactory, tmp_path: Path
) -> None:
    store = store_factory(tmp_path)
    _create_session(store, "session-1", "workspace-1", tmp_path / "one")
    _, running, step = _prepare_step(store, tmp_path)
    _send(store, "session-1", "steer-1", delivery="steer")
    request_ready = replace(
        running,
        revision=3,
        current_step=replace(
            step,
            phase="request_ready",
            request_intent=ModelRequestIntent(ModelContext(SystemContent(), ()), "fp"),
        ),
    )
    assert not store.commit_run_transition(
        state=request_ready,
        expected_revision=2,
        updated_at=NOW,
        node=None,
    )
    assert store.load_run_state("operation-1") == running

    terminal = replace(
        running,
        revision=3,
        status="succeeded",
        current_step=None,
        final_assistant_node_id="message-1",
    )
    assert not store.commit_run_transition(
        state=terminal,
        expected_revision=2,
        updated_at=NOW,
        node=None,
    )
    assert store.load_session("session-1").active_operation_id == "operation-1"


def test_commit_request_intent_and_success_ignore_pending_followup(
    store_factory: StoreFactory, tmp_path: Path
) -> None:
    store = store_factory(tmp_path)
    _create_session(store, "session-1", "workspace-1", tmp_path / "one")
    _, running, step = _prepare_step(store, tmp_path)
    _send(store, "session-1", "followup-1", delivery="followup")
    request_ready = replace(
        running,
        revision=3,
        current_step=replace(
            step,
            phase="request_ready",
            request_intent=ModelRequestIntent(ModelContext(SystemContent(), ()), "fp"),
        ),
    )
    assert store.commit_run_transition(
        state=request_ready,
        expected_revision=2,
        updated_at=NOW,
        node=None,
    )


def _stopping_state(running: AgentRunState) -> AgentRunState:
    return replace(
        running,
        revision=running.revision + 1,
        current_step=ModelStepState(
            "step-1",
            1,
            "awaiting_tools",
            0,
            None,
            "message-1",
            (),
        ),
    )


def test_stopping_continue_requires_pending_step_message(
    store_factory: StoreFactory, tmp_path: Path
) -> None:
    store = store_factory(tmp_path)
    _create_session(store, "session-1", "workspace-1", tmp_path / "one")
    _, running, _ = _prepare_step(store, tmp_path)
    stopping = _stopping_state(running)
    assert store.commit_run_transition(
        state=stopping, expected_revision=2, updated_at=NOW, node=None
    )
    continued = replace(
        stopping,
        revision=4,
        current_step=None,
        completed_step_count=1,
    )
    assert not store.commit_run_transition(
        state=continued, expected_revision=3, updated_at=NOW, node=None
    )
    assert store.load_run_state("operation-1") == stopping

    _send(store, "session-1", "steer-1", delivery="steer")
    assert store.commit_run_transition(
        state=continued, expected_revision=3, updated_at=NOW, node=None
    )


def test_stopping_continue_does_not_accept_followup_only(
    store_factory: StoreFactory, tmp_path: Path
) -> None:
    store = store_factory(tmp_path)
    _create_session(store, "session-1", "workspace-1", tmp_path / "one")
    _, running, _ = _prepare_step(store, tmp_path)
    stopping = _stopping_state(running)
    assert store.commit_run_transition(
        state=stopping, expected_revision=2, updated_at=NOW, node=None
    )
    _send(store, "session-1", "followup-1", delivery="followup")
    continued = replace(stopping, revision=4, current_step=None, completed_step_count=1)
    assert not store.commit_run_transition(
        state=continued, expected_revision=3, updated_at=NOW, node=None
    )


@pytest.mark.parametrize(
    ("status", "kwargs"),
    (
        ("succeeded", {"final_assistant_node_id": "message-1"}),
        (
            "failed",
            {"error": AgentRunError("failed", "test", retryable=False)},
        ),
        (
            "cancelled",
            {"cancellation": Cancellation("user", NOW)},
        ),
    ),
)
def test_three_terminal_states_clear_active_operation(
    store_factory: StoreFactory,
    tmp_path: Path,
    status: str,
    kwargs: dict[str, Any],
) -> None:
    store = store_factory(tmp_path)
    _create_session(store, "session-1", "workspace-1", tmp_path / "one")
    _, _, initial = _accept_one(store, tmp_path)
    running = replace(initial, revision=2, status="running")
    assert store.commit_run_transition(
        state=running, expected_revision=1, updated_at=NOW, node=None
    )
    terminal = replace(running, revision=3, status=status, **kwargs)
    assert store.commit_run_transition(
        state=terminal, expected_revision=2, updated_at=NOW, node=None
    )
    assert store.load_run_state("operation-1") == terminal
    assert store.load_session("session-1").active_operation_id is None


def test_delegated_terminal_transition_appends_settled_message_atomically(
    store_factory: StoreFactory, tmp_path: Path
) -> None:
    store = store_factory(tmp_path)
    parent_root = tmp_path / "parent"
    child_root = tmp_path / "child"
    _create_session(store, "parent", "workspace-parent", parent_root)
    _create_session(store, "child", "workspace-child", child_root)
    package = _package()
    store.insert_agent_package_version(package)

    parent_input = _send(store, "parent", "parent-input")
    parent_operation = _operation(
        "parent-operation",
        "parent",
        package,
        parent_input.message_id,
        "workspace-parent",
        parent_root,
    )
    assert store.accept_operation(
        operation=parent_operation,
        state=_queued(parent_operation.operation_id),
        expected_node_id=None,
    )
    child_input = _send(store, "child", "child-input")
    store.insert_delegation(
        AgentDelegation(
            child_session_id="child",
            child_package_version_id=package.package_version_id,
            parent_operation_id=parent_operation.operation_id,
            parent_step_id="step-1",
            parent_tool_call_id="tool-1",
            initial_message_id=child_input.message_id,
            created_at=NOW,
        )
    )
    child_operation = _operation(
        "child-operation",
        "child",
        package,
        child_input.message_id,
        "workspace-child",
        child_root,
    )
    child_state = _queued(child_operation.operation_id)
    assert store.accept_operation(
        operation=child_operation, state=child_state, expected_node_id=None
    )
    running = replace(child_state, revision=2, status="running")
    assert store.commit_run_transition(
        state=running, expected_revision=1, node=None, updated_at=NOW
    )
    final_node = ConversationNode(
        "child-final",
        "child",
        "child-input",
        "agent_message",
        AssistantMessage((TextBlock("done"),)),
        NOW,
    )
    terminal = replace(
        running,
        revision=3,
        status="succeeded",
        final_assistant_node_id=final_node.node_id,
    )
    assert store.commit_run_transition(
        state=terminal,
        expected_revision=2,
        node=final_node,
        updated_at=NOW,
    )
    pending = store.list_pending(session_id="parent")
    assert len(pending) == 1
    assert pending[0].message_id == agent_settled_message_id("child", "child-operation")
    assert pending[0].delivery == "steer"
    assert pending[0].source == AgentSettledMessageSource("child", "child-operation")
    assert isinstance(pending[0].message.content[0], TextBlock)
    assert json.loads(pending[0].message.content[0].text) == {
        "child_session_id": "child",
        "status": "succeeded",
        "type": "agent_settled",
    }
    assert pending[0].message.content[1] == TextBlock("done")
    assert store.load_session("child").active_operation_id is None


def test_agent_settled_source_codec_is_strict() -> None:
    message = InboxMessage(
        message_id="settled-message",
        session_id="parent",
        sequence=1,
        delivery="steer",
        message=UserMessage((TextBlock("done"),)),
        source=AgentSettledMessageSource("child", "child-operation"),
        created_at=NOW,
    )
    restored = InboxMessage.from_json(message.to_json())
    assert restored.source == message.source
    assert restored.message_id == message.message_id


def _setup_running_delegated_child(store: Store, root: Path, suffix: str = ""):
    parent_root = root / f"parent{suffix}"
    child_root = root / f"child{suffix}"
    _create_session(store, f"parent{suffix}", f"workspace-parent{suffix}", parent_root)
    _create_session(store, f"child{suffix}", f"workspace-child{suffix}", child_root)
    package = _package()
    store.insert_agent_package_version(package)
    parent_input = _send(store, f"parent{suffix}", f"parent-input{suffix}")
    parent_operation = _operation(
        f"parent-operation{suffix}",
        f"parent{suffix}",
        package,
        parent_input.message_id,
        f"workspace-parent{suffix}",
        parent_root,
    )
    parent_state = _queued(parent_operation.operation_id)
    assert store.accept_operation(
        operation=parent_operation, state=parent_state, expected_node_id=None
    )
    child_input = _send(store, f"child{suffix}", f"child-input{suffix}")
    store.insert_delegation(
        AgentDelegation(
            child_session_id=f"child{suffix}",
            child_package_version_id=package.package_version_id,
            parent_operation_id=parent_operation.operation_id,
            parent_step_id=f"step{suffix}",
            parent_tool_call_id=f"tool{suffix}",
            initial_message_id=child_input.message_id,
            created_at=NOW,
        )
    )
    child_operation = _operation(
        f"child-operation{suffix}",
        f"child{suffix}",
        package,
        child_input.message_id,
        f"workspace-child{suffix}",
        child_root,
    )
    child_state = _queued(child_operation.operation_id)
    assert store.accept_operation(
        operation=child_operation, state=child_state, expected_node_id=None
    )
    running = replace(child_state, revision=2, status="running")
    assert store.commit_run_transition(
        state=running, expected_revision=1, node=None, updated_at=NOW
    )
    return parent_operation, child_operation, running


@pytest.mark.parametrize(
    ("status", "kwargs", "expected"),
    (
        (
            "failed",
            {"error": AgentRunError("child_failed", "bad child", False)},
            '"code":"child_failed"',
        ),
        (
            "cancelled",
            {"cancellation": Cancellation("parent requested", NOW)},
            '"message":"parent requested"',
        ),
    ),
)
def test_settled_failed_and_cancelled_messages_have_stable_summary(
    store_factory: StoreFactory,
    tmp_path: Path,
    status: str,
    kwargs: dict[str, Any],
    expected: str,
) -> None:
    store = store_factory(tmp_path)
    _, child_operation, running = _setup_running_delegated_child(store, tmp_path)
    terminal = replace(running, revision=3, status=status, **kwargs)
    assert store.commit_run_transition(
        state=terminal, expected_revision=2, node=None, updated_at=NOW
    )
    pending = store.list_pending(session_id="parent")
    assert len(pending) == 1
    assert isinstance(pending[0].message.content[0], TextBlock)
    assert json.loads(pending[0].message.content[0].text) == {
        "child_session_id": "child",
        "status": status,
        "type": "agent_settled",
    }
    assert isinstance(pending[0].message.content[1], TextBlock)
    assert expected in pending[0].message.content[1].text
    assert child_operation.operation_id in pending[0].source.sender_operation_id


def test_stale_terminal_cas_does_not_append_settled_message(
    store_factory: StoreFactory, tmp_path: Path
) -> None:
    store = store_factory(tmp_path)
    _, _, running = _setup_running_delegated_child(store, tmp_path)
    stale = replace(
        running,
        revision=1,
        status="failed",
        error=AgentRunError("stale", "stale", False),
    )
    assert not store.commit_run_transition(
        state=stale, expected_revision=0, node=None, updated_at=NOW
    )
    assert store.load_run_state(running.operation_id) == running
    assert store.list_pending(session_id="parent") == ()


def test_settled_construction_failure_rolls_back_state_session_and_inbox(
    store_factory: StoreFactory, tmp_path: Path
) -> None:
    store = store_factory(tmp_path)
    _, _, running = _setup_running_delegated_child(store, tmp_path)
    invalid_success = replace(
        running,
        revision=3,
        status="succeeded",
        final_assistant_node_id="missing-final",
    )
    with pytest.raises(StorageIntegrityError):
        store.commit_run_transition(
            state=invalid_success, expected_revision=2, node=None, updated_at=NOW
        )
    assert store.load_run_state(running.operation_id) == running
    assert store.load_session("child").active_operation_id == running.operation_id
    assert store.list_pending(session_id="parent") == ()


def test_archived_parent_failure_rolls_back_terminal_transition(
    store_factory: StoreFactory, tmp_path: Path
) -> None:
    store = store_factory(tmp_path)
    _, _, running = _setup_running_delegated_child(store, tmp_path)
    if isinstance(store, InMemoryRuntimeStore):
        store._sessions["parent"] = replace(
            store._sessions["parent"], active_operation_id=None, archived_at=NOW
        )
    else:
        with store._connect() as connection:
            connection.execute(
                "UPDATE conversation_sessions SET active_operation_id = NULL, archived_at = ? WHERE session_id = ?",
                (NOW.isoformat(), "parent"),
            )
    terminal = replace(
        running,
        revision=3,
        status="failed",
        error=AgentRunError("failed", "failed", False),
    )
    with pytest.raises(StorageConflictError):
        store.commit_run_transition(
            state=terminal, expected_revision=2, node=None, updated_at=NOW
        )
    assert store.load_run_state(running.operation_id) == running
    assert store.load_session("child").active_operation_id == running.operation_id
    assert store.list_pending(session_id="parent") == ()


def test_archive_rechecks_descendant_run_state_without_active_pointer(
    store_factory: StoreFactory, tmp_path: Path
) -> None:
    store = store_factory(tmp_path)
    _, _, running = _setup_running_delegated_child(store, tmp_path)
    if isinstance(store, InMemoryRuntimeStore):
        store._sessions["parent"] = replace(
            store._sessions["parent"], active_operation_id=None
        )
        store._sessions["child"] = replace(
            store._sessions["child"], active_operation_id=None
        )
    else:
        with store._connect() as connection:
            connection.execute(
                "UPDATE conversation_sessions SET active_operation_id = NULL WHERE session_id IN (?, ?)",
                ("parent", "child"),
            )
    assert store.load_run_state(running.operation_id) == running
    with pytest.raises(StorageIntegrityError):
        store.archive_session(session_id="parent", archived_at=NOW)
    assert store.load_session("parent").archived_at is None


def test_parallel_children_use_parent_sequence_in_actual_terminal_order(
    store_factory: StoreFactory, tmp_path: Path
) -> None:
    store = store_factory(tmp_path)
    parent_root = tmp_path / "parent"
    _create_session(store, "parent", "workspace-parent", parent_root)
    package = _package()
    store.insert_agent_package_version(package)
    parent_input = _send(store, "parent", "parent-input")
    parent_operation = _operation(
        "parent-operation",
        "parent",
        package,
        parent_input.message_id,
        "workspace-parent",
        parent_root,
    )
    assert store.accept_operation(
        operation=parent_operation,
        state=_queued(parent_operation.operation_id),
        expected_node_id=None,
    )
    children = []
    for index in (1, 2):
        child_root = tmp_path / f"child-{index}"
        _create_session(store, f"child-{index}", f"workspace-child-{index}", child_root)
        child_input = _send(store, f"child-{index}", f"child-input-{index}")
        store.insert_delegation(
            AgentDelegation(
                child_session_id=f"child-{index}",
                child_package_version_id=package.package_version_id,
                parent_operation_id=parent_operation.operation_id,
                parent_step_id=f"step-{index}",
                parent_tool_call_id=f"tool-{index}",
                initial_message_id=child_input.message_id,
                created_at=NOW,
            )
        )
        operation = _operation(
            f"child-operation-{index}",
            f"child-{index}",
            package,
            child_input.message_id,
            f"workspace-child-{index}",
            child_root,
        )
        state = _queued(operation.operation_id)
        assert store.accept_operation(
            operation=operation, state=state, expected_node_id=None
        )
        running = replace(state, revision=2, status="running")
        assert store.commit_run_transition(
            state=running, expected_revision=1, node=None, updated_at=NOW
        )
        children.append(running)
    for running in reversed(children):
        terminal = replace(
            running,
            revision=3,
            status="failed",
            error=AgentRunError("failed", running.operation_id, False),
        )
        assert store.commit_run_transition(
            state=terminal, expected_revision=2, node=None, updated_at=NOW
        )
    pending = store.list_pending(session_id="parent")
    assert [item.sequence for item in pending] == [2, 3]
    assert [item.source.sender_operation_id for item in pending] == [
        "child-operation-2",
        "child-operation-1",
    ]


def test_archived_ancestor_rejects_child_send_and_accept(
    store_factory: StoreFactory, tmp_path: Path
) -> None:
    store = store_factory(tmp_path)
    _, child_operation, running = _setup_running_delegated_child(store, tmp_path)
    if isinstance(store, InMemoryRuntimeStore):
        store._sessions["parent"] = replace(
            store._sessions["parent"], active_operation_id=None, archived_at=NOW
        )
        store._sessions["child"] = replace(
            store._sessions["child"], active_operation_id=None
        )
    else:
        with store._connect() as connection:
            connection.execute(
                "UPDATE conversation_sessions SET active_operation_id = NULL, archived_at = ? WHERE session_id = ?",
                (NOW.isoformat(), "parent"),
            )
            connection.execute(
                "UPDATE conversation_sessions SET active_operation_id = NULL WHERE session_id = ?",
                ("child",),
            )
    with pytest.raises(StorageIntegrityError):
        _send(store, "child", "after-archive")

    # 模拟恢复扫描已发现的 pending Inbox；accept 仍必须重新检查 ancestor。
    pending = InboxMessage(
        message_id="pending-after-archive",
        session_id="child",
        sequence=2,
        delivery="followup",
        message=UserMessage((TextBlock("pending"),)),
        source=UserMessageSource(),
        created_at=NOW,
    )
    if isinstance(store, InMemoryRuntimeStore):
        store._inbox[pending.message_id] = pending
    else:
        with store._connect() as connection:
            connection.execute(
                """
                INSERT INTO agent_inbox_messages (
                    message_id, session_id, sequence, delivery, message_json,
                    status, claimed_operation_id, claimed_step_id, outcome_reason,
                    created_at, handled_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    pending.message_id,
                    pending.session_id,
                    pending.sequence,
                    pending.delivery,
                    pending.message_payload_json(),
                    pending.status,
                    None,
                    None,
                    None,
                    pending.created_at.isoformat(),
                    None,
                ),
            )
    next_operation = _operation(
        "child-after-archive",
        "child",
        _package(),
        pending.message_id,
        "workspace-child",
        tmp_path / "child",
    )
    with pytest.raises(StorageIntegrityError):
        store.accept_operation(
            operation=next_operation,
            state=_queued(next_operation.operation_id),
            expected_node_id=store.load_session("child").active_node_id,
        )
    assert store.load_run_state(running.operation_id) == running
    assert child_operation.operation_id == running.operation_id


def test_cross_session_operation_and_artifact_references_are_rejected(
    store_factory: StoreFactory, tmp_path: Path
) -> None:
    store = store_factory(tmp_path)
    _create_session(store, "session-1", "workspace-1", tmp_path / "one")
    _create_session(store, "session-2", "workspace-2", tmp_path / "two")
    package = _package()
    store.insert_agent_package_version(package)
    node = ConversationNode(
        "node-1",
        "session-1",
        None,
        "agent_message",
        UserMessage((TextBlock("one"),)),
        NOW,
    )
    assert store.append_node(node=node, expected_node_id=None)
    cross_session = _operation(
        "operation-2", "session-2", package, "node-1", "workspace-2", tmp_path / "two"
    )
    assert not store.accept_operation(
        operation=cross_session,
        state=_queued("operation-2"),
        expected_node_id=None,
    )

    artifact_id = "artifact_" + "a" * 64
    artifact_message = UserMessage(
        (ArtifactBlock(ArtifactReference(artifact_id, "image/png")),)
    )
    with pytest.raises(StorageIntegrityError):
        _send(store, "session-1", "artifact-message", message=artifact_message)
    store.insert_artifact(Artifact(artifact_id, 3, NOW))
    stored = _send(store, "session-1", "artifact-message", message=artifact_message)
    assert stored.message == artifact_message
    assert store.load_artifact(artifact_id) == Artifact(artifact_id, 3, NOW)


def test_delete_and_delete_tree_enforce_delegation_preconditions(
    store_factory: StoreFactory, tmp_path: Path
) -> None:
    store = store_factory(tmp_path)
    parent_root = tmp_path / "parent"
    child_root = tmp_path / "child"
    _create_session(store, "parent", "parent-workspace", parent_root)
    _create_session(store, "child", "child-workspace", child_root)
    package = _package()
    store.insert_agent_package_version(package)
    _send(store, "parent", "parent-message")
    parent_operation = _operation(
        "parent-operation",
        "parent",
        package,
        "parent-message",
        "parent-workspace",
        parent_root,
    )
    assert store.accept_operation(
        operation=parent_operation,
        state=_queued("parent-operation"),
        expected_node_id=None,
    )
    assert store.commit_run_transition(
        state=AgentRunState(
            "parent-operation",
            2,
            "running",
            None,
            0,
            None,
            None,
            None,
            None,
        ),
        expected_revision=1,
        updated_at=NOW,
        node=None,
    )
    assert store.commit_run_transition(
        state=AgentRunState(
            "parent-operation",
            3,
            "succeeded",
            None,
            1,
            None,
            "parent-message",
            None,
            None,
        ),
        expected_revision=2,
        updated_at=NOW,
        node=None,
    )
    child_message = _send(store, "child", "child-message")
    store.insert_delegation(
        AgentDelegation(
            child_session_id="child",
            child_package_version_id=package.package_version_id,
            parent_operation_id="parent-operation",
            parent_step_id="step-1",
            parent_tool_call_id="tool-call-1",
            initial_message_id=child_message.message_id,
            created_at=NOW,
        )
    )
    store.discard_message(message_id="child-message", reason="test", handled_at=NOW)
    store.archive_session(session_id="parent", archived_at=NOW)
    store.archive_session(session_id="child", archived_at=NOW)

    with pytest.raises(StorageIntegrityError):
        store.delete_session(session_id="parent")
    with pytest.raises(StorageIntegrityError):
        store.delete_session(session_id="child")

    store.delete_session_tree(session_id="parent")
    assert store.load_session("parent") is None
    assert store.load_session("child") is None
    assert store.load_delegation("child") is None


def test_startup_and_recovery_paths_do_not_query_operation_history() -> None:
    import inspect

    from pickel.app.runtime_host import RuntimeHost
    from pickel.runtime.agent_driver import AgentDriver
    from pickel.runtime.operation_driver import OperationDriver

    for owner in (AgentDriver, OperationDriver, RuntimeHost):
        assert "list_operations(" not in inspect.getsource(owner)
