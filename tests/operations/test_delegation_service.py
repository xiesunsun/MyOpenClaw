"""Delegation durable acceptance 的双 Store 合同。"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

import pytest

from pickel.agents.agent_package import (
    AgentRuntimePolicy,
    ImplementationRef,
    ModelPolicy,
    ModelVersion,
    WorkspacePolicy,
    build_agent_package_version,
)
from pickel.conversations.agent_message import UserMessage
from pickel.conversations.content_blocks import TextBlock
from pickel.conversations.conversation_session import ConversationSession
from pickel.inbox.message import AgentMessageSource
from pickel.operations.agent_run_state import (
    AgentRunState,
    DelegateAgentIntent,
    ModelStepState,
    ToolCallState,
)
from pickel.operations.delegation_service import DelegationService
from pickel.operations.session_operation import SessionOperation
from pickel.persistence.errors import StorageConflictError, StorageIntegrityError
from pickel.persistence.in_memory_runtime_store import InMemoryRuntimeStore
from pickel.persistence.sqlite_runtime_store import SQLiteRuntimeStore
from pickel.workspaces.workspace import Workspace
from pickel.workspaces.workspace_binding import WorkspaceBinding

NOW = datetime(2026, 8, 25, tzinfo=timezone.utc)
Store = Any


@pytest.fixture(params=("memory", "sqlite"))
def store(request: pytest.FixtureRequest, tmp_path: Path) -> Store:
    if request.param == "memory":
        return InMemoryRuntimeStore()
    return SQLiteRuntimeStore(tmp_path / "runtime.db")


def _package(agent_id: str, *, max_depth: int = 3):
    return build_agent_package_version(
        agent_id=agent_id,
        format_version=1,
        behavior_instruction=agent_id,
        model_policy=ModelPolicy(
            primary=ModelVersion(
                provider="test",
                model="test",
                api_base=None,
                temperature=None,
                max_input_tokens=None,
                max_output_tokens=32,
                provider_options={},
                provider_implementation=ImplementationRef("provider", "test"),
                required_secret_refs=(),
            )
        ),
        runtime_policy=AgentRuntimePolicy(3, 8, max_depth),
        workspace_policy=WorkspacePolicy("workspace"),
        skills=(),
        tools=(),
        extensions=(),
        created_at=NOW,
    )


def _setup(
    store: Store,
    root: Path,
    *,
    max_depth: int = 3,
    call_status: str = "intent_recorded",
    child_package_id: str | None = None,
    include_intent: bool = True,
    binding_root: Path | None = None,
):
    root.mkdir(parents=True, exist_ok=True)
    store.create_session(
        workspace=Workspace("workspace-1", root, NOW),
        session=ConversationSession(
            session_id="session-1",
            agent_id="parent-agent",
            workspace_id="workspace-1",
            cwd=root,
            active_node_id=None,
            active_operation_id=None,
            title=None,
            title_source=None,
            created_at=NOW,
            updated_at=NOW,
            archived_at=None,
        ),
    )
    parent_package = _package("parent-agent", max_depth=max_depth)
    child_package = _package("child-agent")
    store.insert_agent_package_version(parent_package)
    store.insert_agent_package_version(child_package)
    input_message = store.send_message(
        message_id="message-1",
        session_id="session-1",
        delivery="followup",
        message=UserMessage((TextBlock("input"),)),
        source=AgentMessageSource(
            sender_session_id="sender",
            sender_operation_id="sender-op",
            form="followup",
        ),
        created_at=NOW,
    )
    operation = SessionOperation(
        operation_id="operation-1",
        session_id="session-1",
        agent_package_version_id=parent_package.package_version_id,
        workspace_binding=WorkspaceBinding(
            "workspace-1", binding_root or root, binding_root or root
        ),
        input_node_id=input_message.message_id,
        accepted_at=NOW,
    )
    state = AgentRunState(
        operation_id="operation-1",
        revision=1,
        status="queued",
        waiting_reason=None,
        completed_step_count=0,
        current_step=None,
        final_assistant_node_id=None,
        error=None,
        cancellation=None,
    )
    assert store.accept_operation(
        operation=operation, state=state, expected_node_id=None
    )
    call = ToolCallState(
        tool_call_id="tool-1",
        tool_name="delegate",
        arguments={},
        status=call_status,
        approval=None,
        replay_policy="safe",
        execution_intent=(
            DelegateAgentIntent(child_package_id or child_package.package_version_id)
            if include_intent
            else None
        ),
        decision_reason=None,
        result_node_id=None,
        is_error=None,
    )
    step = ModelStepState("step-1", 1, "awaiting_tools", 0, None, "message-1", (call,))
    running = replace(state, revision=2, status="running", current_step=step)
    assert store.commit_run_transition(
        state=running, expected_revision=1, node=None, updated_at=NOW
    )
    return parent_package, child_package


def _service(store: Store, *, ids: list[str] | None = None) -> DelegationService:
    values = iter(ids or ("child-session-1", "initial-message-1"))
    return DelegationService(
        store=store,
        child_session_id_factory=lambda: next(values),
        message_id_factory=lambda: next(values),
        now=lambda: NOW,
    )


def test_start_delegation_accepts_three_facts_and_is_idempotent(
    store: Store, tmp_path: Path
) -> None:
    _, child_package = _setup(store, tmp_path / "workspace")
    message = UserMessage((TextBlock("child input"),))
    first = _service(store).start_delegation("operation-1", "step-1", "tool-1", message)

    child = store.load_session(first.child_session_id)
    initial = store.load_message(first.initial_message_id)
    assert child is not None
    assert child.agent_id == "child-agent"
    assert child.workspace_id == "workspace-1"
    assert child.cwd == tmp_path / "workspace"
    assert initial is not None
    assert initial.status == "pending"
    assert initial.sequence == 1
    assert initial.source == AgentMessageSource(
        sender_session_id="session-1",
        sender_operation_id="operation-1",
        form="followup",
    )
    assert store.list_operations(session_id=first.child_session_id) == ()
    assert store.load_node(first.initial_message_id) is None
    assert child.active_node_id is None
    assert child.active_operation_id is None
    assert child.title is None
    assert child.title_source is None
    assert child.archived_at is None
    assert initial.delivery == "followup"

    child_operation = SessionOperation(
        operation_id="child-operation-1",
        session_id=first.child_session_id,
        agent_package_version_id=child_package.package_version_id,
        workspace_binding=WorkspaceBinding(
            "workspace-1", tmp_path / "workspace", tmp_path / "workspace"
        ),
        input_node_id=first.initial_message_id,
        accepted_at=NOW,
    )
    child_state = AgentRunState(
        operation_id="child-operation-1",
        revision=1,
        status="queued",
        waiting_reason=None,
        completed_step_count=0,
        current_step=None,
        final_assistant_node_id=None,
        error=None,
        cancellation=None,
    )
    assert store.accept_operation(
        operation=child_operation,
        state=child_state,
        expected_node_id=None,
    )
    assert store.load_message(first.initial_message_id).status == "claimed"

    retry = _service(store, ids=["different-session", "different-message"])
    assert retry.start_delegation("operation-1", "step-1", "tool-1", message) == first

    with pytest.raises(StorageConflictError):
        _service(store, ids=["another-session", "another-message"]).start_delegation(
            "operation-1", "step-1", "tool-1", UserMessage((TextBlock("changed"),))
        )


def test_delegation_depth_zero_is_rejected_without_residue(
    store: Store, tmp_path: Path
) -> None:
    _setup(store, tmp_path / "workspace", max_depth=0)
    with pytest.raises(StorageConflictError):
        _service(store).start_delegation(
            "operation-1", "step-1", "tool-1", UserMessage((TextBlock("child"),))
        )
    assert store.load_session("child-session-1") is None
    assert store.load_message("initial-message-1") is None


@pytest.mark.parametrize(
    ("setup_kwargs", "step_id", "tool_id"),
    (
        ({}, "wrong-step", "tool-1"),
        ({}, "step-1", "wrong-tool"),
        ({"call_status": "ready"}, "step-1", "tool-1"),
        ({"include_intent": False}, "step-1", "tool-1"),
        ({"child_package_id": "missing-package"}, "step-1", "tool-1"),
    ),
)
def test_invalid_parent_facts_leave_no_child_residue(
    store: Store,
    tmp_path: Path,
    setup_kwargs: dict[str, object],
    step_id: str,
    tool_id: str,
) -> None:
    _setup(store, tmp_path / "workspace", **setup_kwargs)
    with pytest.raises((StorageConflictError, StorageIntegrityError)):
        _service(store).start_delegation(
            "operation-1", step_id, tool_id, UserMessage((TextBlock("child"),))
        )
    assert store.load_session("child-session-1") is None
    assert store.load_message("initial-message-1") is None
    assert store.list_delegations(parent_operation_id="operation-1") == ()


def test_parent_active_operation_and_workspace_binding_are_rechecked(
    store: Store, tmp_path: Path
) -> None:
    root = tmp_path / "workspace"
    _setup(store, root)
    if isinstance(store, InMemoryRuntimeStore):
        session = store.load_session("session-1")
        assert session is not None
        store._sessions["session-1"] = replace(session, active_operation_id=None)
    else:
        with store._connect() as connection:
            connection.execute(
                "UPDATE conversation_sessions SET active_operation_id = NULL WHERE session_id = ?",
                ("session-1",),
            )
    with pytest.raises(StorageConflictError):
        _service(store).start_delegation(
            "operation-1", "step-1", "tool-1", UserMessage((TextBlock("child"),))
        )
    assert store.load_session("child-session-1") is None
    assert store.load_message("initial-message-1") is None
    assert store.list_delegations(parent_operation_id="operation-1") == ()


def test_parent_workspace_binding_drift_leaves_no_child_residue(
    store: Store, tmp_path: Path
) -> None:
    _setup(
        store,
        tmp_path / "workspace",
        binding_root=tmp_path / "other-workspace",
    )
    with pytest.raises(StorageIntegrityError):
        _service(store).start_delegation(
            "operation-1", "step-1", "tool-1", UserMessage((TextBlock("child"),))
        )
    assert store.load_session("child-session-1") is None
    assert store.load_message("initial-message-1") is None
    assert store.list_delegations(parent_operation_id="operation-1") == ()


def test_concurrent_acceptance_keeps_one_delegation(
    store: Store, tmp_path: Path
) -> None:
    _setup(store, tmp_path / "workspace")
    message = UserMessage((TextBlock("child"),))

    def accept(index: int):
        return _service(
            store, ids=[f"child-session-{index}", f"initial-message-{index}"]
        ).start_delegation("operation-1", "step-1", "tool-1", message)

    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(accept, range(4)))
    assert len({item.child_session_id for item in results}) == 1
    assert len(store.list_delegations(parent_operation_id="operation-1")) == 1
