"""Delegation durable acceptance 的双 Store 合同。"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
import json
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
from pickel.conversations.agent_message import (
    AssistantMessage,
    UserMessage,
    agent_message_to_dict,
)
from pickel.conversations.content_blocks import TextBlock
from pickel.conversations.conversation_session import ConversationSession
from pickel.inbox.message import AgentMessageSource
from pickel.operations.agent_run_state import (
    AgentRunState,
    AgentRunError,
    Cancellation,
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


def _switch_parent_call_to_send_message(
    store: Store,
    child_session_id: str,
    *,
    operation_id: str = "operation-1",
    step_id: str = "step-1",
    tool_call_id: str = "tool-send",
    message_text: str = "continue",
) -> None:
    current = store.load_run_state(operation_id)
    assert current is not None
    call = ToolCallState(
        tool_call_id=tool_call_id,
        tool_name="send_message",
        arguments={"child_session_id": child_session_id, "message": message_text},
        status="intent_recorded",
        approval=None,
        replay_policy="safe",
        execution_intent=None,
        decision_reason=None,
        result_node_id=None,
        is_error=None,
    )
    if current.current_step is None:
        switched = replace(
            current,
            revision=current.revision + 1,
            status="running",
            current_step=ModelStepState(
                step_id=step_id,
                step_sequence=1,
                phase="awaiting_tools",
                request_attempt=0,
                request_intent=None,
                assistant_message_node_id="message-2",
                tool_calls=(call,),
            ),
        )
    else:
        switched = replace(
            current,
            current_step=replace(current.current_step, tool_calls=(call,)),
        )
    if isinstance(store, InMemoryRuntimeStore):
        store._run_states[operation_id] = switched
        return
    with store._connect() as connection:
        connection.execute(
            """
            UPDATE agent_run_states
            SET revision = ?, status = ?, current_step_json = ?
            WHERE operation_id = ?
            """,
            (
                switched.revision,
                switched.status,
                json.dumps(
                    switched.current_step.content_dict(),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                operation_id,
            ),
        )


def _switch_parent_call_to_list_agents(
    store: Store,
    *,
    operation_id: str = "operation-1",
    step_id: str = "step-1",
    tool_call_id: str = "tool-list",
) -> None:
    current = store.load_run_state(operation_id)
    assert current is not None
    call = ToolCallState(
        tool_call_id=tool_call_id,
        tool_name="list_agents",
        arguments={},
        status="intent_recorded",
        approval=None,
        replay_policy="safe",
        execution_intent=None,
        decision_reason=None,
        result_node_id=None,
        is_error=None,
    )
    assert current.current_step is not None
    switched = replace(
        current,
        current_step=replace(current.current_step, step_id=step_id, tool_calls=(call,)),
    )
    if isinstance(store, InMemoryRuntimeStore):
        store._run_states[operation_id] = switched
        return
    with store._connect() as connection:
        connection.execute(
            """
            UPDATE agent_run_states
            SET revision = ?, status = ?, current_step_json = ?
            WHERE operation_id = ?
            """,
            (
                switched.revision,
                switched.status,
                json.dumps(
                    switched.current_step.content_dict(),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                operation_id,
            ),
        )


def test_list_child_agents_returns_ready_without_mutating_facts(
    store: Store, tmp_path: Path
) -> None:
    _setup(store, tmp_path / "workspace")
    delegation = _service(store).start_delegation(
        "operation-1", "step-1", "tool-1", UserMessage((TextBlock("child"),))
    )
    _switch_parent_call_to_list_agents(store)
    before = (
        store.load_session(delegation.child_session_id),
        store.list_operations(session_id=delegation.child_session_id),
        store.list_pending(session_id=delegation.child_session_id),
        store.list_delegations(parent_operation_id="operation-1"),
    )

    snapshots = DelegationService(store=store).list_child_agents(
        "operation-1", "step-1", "tool-list"
    )

    assert len(snapshots) == 1
    assert snapshots[0].to_dict() == {
        "child_session_id": delegation.child_session_id,
        "agent_id": "child-agent",
        "status": "ready",
        "operation_id": None,
        "waiting_reason": None,
        "completed_step_count": 0,
        "final_assistant_node_id": None,
        "error": None,
    }
    assert before == (
        store.load_session(delegation.child_session_id),
        store.list_operations(session_id=delegation.child_session_id),
        store.list_pending(session_id=delegation.child_session_id),
        store.list_delegations(parent_operation_id="operation-1"),
    )


def _accept_child_operation(
    store: Store,
    delegation_child_session_id: str,
    child_package_id: str,
    root: Path,
    *,
    operation_id: str = "child-operation",
    input_node_id: str = "initial-message-1",
) -> str:
    operation = SessionOperation(
        operation_id=operation_id,
        session_id=delegation_child_session_id,
        agent_package_version_id=child_package_id,
        workspace_binding=WorkspaceBinding("workspace-1", root, root),
        input_node_id=input_node_id,
        accepted_at=NOW,
    )
    state = AgentRunState(
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
    assert store.accept_operation(
        operation=operation, state=state, expected_node_id=None
    )
    return operation_id


def _set_child_state(
    store: Store,
    operation_id: str,
    state: AgentRunState,
    *,
    active: bool,
    session_id: str = "child-session-1",
) -> None:
    if isinstance(store, InMemoryRuntimeStore):
        store._run_states[operation_id] = state
        session = store.load_session(session_id)
        assert session is not None
        store._sessions[session_id] = replace(
            session, active_operation_id=operation_id if active else None
        )
        return
    with store._connect() as connection:
        connection.execute(
            """
            UPDATE agent_run_states
            SET revision = ?, status = ?, waiting_reason = ?,
                completed_step_count = ?, current_step_json = ?,
                final_assistant_node_id = ?, error_json = ?,
                cancellation_json = ?, updated_at = ?
            WHERE operation_id = ?
            """,
            (
                state.revision,
                state.status,
                state.waiting_reason,
                state.completed_step_count,
                (
                    json.dumps(state.current_step.content_dict(), ensure_ascii=False)
                    if state.current_step is not None
                    else None
                ),
                state.final_assistant_node_id,
                (
                    json.dumps(state.to_dict()["error"], ensure_ascii=False)
                    if state.error is not None
                    else None
                ),
                (
                    json.dumps(state.to_dict()["cancellation"], ensure_ascii=False)
                    if state.cancellation is not None
                    else None
                ),
                NOW.isoformat(),
                operation_id,
            ),
        )
        connection.execute(
            "UPDATE conversation_sessions SET active_operation_id = ? WHERE session_id = ?",
            (operation_id if active else None, session_id),
        )


def _switch_child_call_to_report(
    store: Store,
    output: str = "finding",
    *,
    operation_id: str = "child-operation",
) -> None:
    current = store.load_run_state(operation_id)
    assert current is not None
    call = ToolCallState(
        tool_call_id="report-tool",
        tool_name="report",
        arguments={"output": output},
        status="intent_recorded",
        approval=None,
        replay_policy="safe",
        execution_intent=None,
        decision_reason=None,
        result_node_id=None,
        is_error=None,
    )
    switched = replace(
        current,
        revision=current.revision + 1,
        status="running",
        current_step=ModelStepState(
            step_id="report-step",
            step_sequence=1,
            phase="awaiting_tools",
            request_attempt=0,
            request_intent=None,
            assistant_message_node_id="child-assistant",
            tool_calls=(call,),
        ),
    )
    if isinstance(store, InMemoryRuntimeStore):
        store._run_states[operation_id] = switched
        return
    with store._connect() as connection:
        connection.execute(
            """
            UPDATE agent_run_states
            SET revision = ?, status = ?, current_step_json = ?
            WHERE operation_id = ?
            """,
            (
                switched.revision,
                switched.status,
                json.dumps(
                    switched.current_step.content_dict(),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                operation_id,
            ),
        )


def _switch_child_call_to_delegate(
    store: Store,
    child_package_id: str,
    *,
    operation_id: str = "child-operation",
    step_id: str = "delegate-step",
    tool_call_id: str = "delegate-tool",
) -> None:
    current = store.load_run_state(operation_id)
    assert current is not None
    call = ToolCallState(
        tool_call_id=tool_call_id,
        tool_name="delegate_agent",
        arguments={},
        status="intent_recorded",
        approval=None,
        replay_policy="safe",
        execution_intent=DelegateAgentIntent(child_package_id),
        decision_reason=None,
        result_node_id=None,
        is_error=None,
    )
    switched = replace(
        current,
        revision=current.revision + 1,
        status="running",
        current_step=ModelStepState(
            step_id=step_id,
            step_sequence=1,
            phase="awaiting_tools",
            request_attempt=0,
            request_intent=None,
            assistant_message_node_id="child-assistant",
            tool_calls=(call,),
        ),
    )
    if isinstance(store, InMemoryRuntimeStore):
        store._run_states[operation_id] = switched
        return
    with store._connect() as connection:
        connection.execute(
            """
            UPDATE agent_run_states
            SET revision = ?, status = ?, current_step_json = ?
            WHERE operation_id = ?
            """,
            (
                switched.revision,
                switched.status,
                json.dumps(
                    switched.current_step.content_dict(),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                operation_id,
            ),
        )


def _clear_active_operation(store: Store, session_id: str) -> None:
    session = store.load_session(session_id)
    assert session is not None
    if isinstance(store, InMemoryRuntimeStore):
        store._sessions[session_id] = replace(session, active_operation_id=None)
        return
    with store._connect() as connection:
        connection.execute(
            "UPDATE conversation_sessions SET active_operation_id = NULL WHERE session_id = ?",
            (session_id,),
        )


def test_send_child_report_is_steer_and_idempotent(
    store: Store, tmp_path: Path
) -> None:
    _, child_package = _setup(store, tmp_path / "workspace")
    delegation = _service(store).start_delegation(
        "operation-1", "step-1", "tool-1", UserMessage((TextBlock("child"),))
    )
    _accept_child_operation(
        store,
        delegation.child_session_id,
        child_package.package_version_id,
        tmp_path / "workspace",
    )
    _switch_child_call_to_report(store)

    service = DelegationService(store=store, now=lambda: NOW)
    first = service.send_child_report(
        "child-operation", "report-step", "report-tool", "finding"
    )
    assert first.session_id == "session-1"
    assert first.delivery == "steer"
    assert first.sequence == 2
    assert first.source == AgentMessageSource(
        sender_session_id=delegation.child_session_id,
        sender_operation_id="child-operation",
        form="steer",
    )
    assert first.message == UserMessage(
        (TextBlock("Background subagent child-session-1 reported:\nfinding"),)
    )
    with pytest.raises(StorageConflictError):
        service.send_child_report(
            "child-operation", "report-step", "report-tool", "different"
        )
    assert (
        service.send_child_report(
            "child-operation", "report-step", "report-tool", "finding"
        )
        == first
    )


def test_send_child_report_replays_after_claim_and_parent_archive(
    store: Store, tmp_path: Path
) -> None:
    _, child_package = _setup(store, tmp_path / "workspace")
    delegation = _service(store).start_delegation(
        "operation-1", "step-1", "tool-1", UserMessage((TextBlock("child"),))
    )
    _accept_child_operation(
        store,
        delegation.child_session_id,
        child_package.package_version_id,
        tmp_path / "workspace",
    )
    _switch_child_call_to_report(store)
    service = DelegationService(store=store, now=lambda: NOW)
    first = service.send_child_report(
        "child-operation", "report-step", "report-tool", "finding"
    )
    assert store.claim_message(
        message_id=first.message_id,
        operation_id="operation-1",
        step_id="step-1",
        handled_at=NOW,
    )
    retry = service.send_child_report(
        "child-operation", "report-step", "report-tool", "finding"
    )
    assert retry == store.load_message(first.message_id)
    _clear_active_operation(store, "session-1")
    store.archive_session(session_id="session-1", archived_at=NOW)
    archived_retry = service.send_child_report(
        "child-operation", "report-step", "report-tool", "finding"
    )
    assert archived_retry == store.load_message(first.message_id)


def test_send_child_report_rejects_root_sender(store: Store, tmp_path: Path) -> None:
    _setup(store, tmp_path / "workspace")
    with pytest.raises(StorageConflictError):
        DelegationService(store=store).send_child_report(
            "operation-1", "step-1", "tool-1", "finding"
        )


def test_send_child_report_rejects_new_report_to_archived_parent(
    store: Store, tmp_path: Path
) -> None:
    _, child_package = _setup(store, tmp_path / "workspace")
    delegation = _service(store).start_delegation(
        "operation-1", "step-1", "tool-1", UserMessage((TextBlock("child"),))
    )
    _accept_child_operation(
        store,
        delegation.child_session_id,
        child_package.package_version_id,
        tmp_path / "workspace",
    )
    _switch_child_call_to_report(store)
    _clear_active_operation(store, "session-1")
    store.archive_session(session_id="session-1", archived_at=NOW)
    with pytest.raises(StorageConflictError):
        DelegationService(store=store).send_child_report(
            "child-operation", "report-step", "report-tool", "finding"
        )


def test_send_child_report_reaches_only_the_direct_parent(
    store: Store, tmp_path: Path
) -> None:
    _, child_package = _setup(store, tmp_path / "workspace")
    first_delegation = _service(store).start_delegation(
        "operation-1", "step-1", "tool-1", UserMessage((TextBlock("child"),))
    )
    _accept_child_operation(
        store,
        first_delegation.child_session_id,
        child_package.package_version_id,
        tmp_path / "workspace",
    )
    _switch_child_call_to_delegate(
        store,
        child_package.package_version_id,
    )
    grandchild = DelegationService(
        store=store,
        child_session_id_factory=lambda: "grandchild-session",
        message_id_factory=lambda: "grandchild-message",
        now=lambda: NOW,
    ).start_delegation(
        "child-operation",
        "delegate-step",
        "delegate-tool",
        UserMessage((TextBlock("grandchild"),)),
    )
    _accept_child_operation(
        store,
        grandchild.child_session_id,
        child_package.package_version_id,
        tmp_path / "workspace",
        operation_id="grandchild-operation",
        input_node_id="grandchild-message",
    )
    _switch_child_call_to_report(
        store,
        "nested finding",
        operation_id="grandchild-operation",
    )

    report_message = DelegationService(store=store, now=lambda: NOW).send_child_report(
        "grandchild-operation", "report-step", "report-tool", "nested finding"
    )
    assert report_message.session_id == first_delegation.child_session_id
    assert report_message.source == AgentMessageSource(
        sender_session_id="grandchild-session",
        sender_operation_id="grandchild-operation",
        form="steer",
    )
    assert store.list_pending(session_id="session-1") == ()


@pytest.mark.parametrize("status", ["queued", "running", "waiting", "cancelling"])
def test_list_child_agents_projects_active_state(
    store: Store, tmp_path: Path, status: str
) -> None:
    _, child_package = _setup(store, tmp_path / "workspace")
    delegation = _service(store).start_delegation(
        "operation-1", "step-1", "tool-1", UserMessage((TextBlock("child"),))
    )
    operation_id = _accept_child_operation(
        store,
        delegation.child_session_id,
        child_package.package_version_id,
        tmp_path / "workspace",
    )
    current = store.load_run_state(operation_id)
    assert current is not None
    kwargs: dict[str, Any] = {
        "status": status,
        "waiting_reason": "tool_approval" if status == "waiting" else None,
        "cancellation": (Cancellation("test", NOW) if status == "cancelling" else None),
    }
    _set_child_state(
        store, operation_id, replace(current, revision=2, **kwargs), active=True
    )
    _switch_parent_call_to_list_agents(store)

    snapshot = DelegationService(store=store).list_child_agents(
        "operation-1", "step-1", "tool-list"
    )[0]
    assert snapshot.status == status
    assert snapshot.operation_id == operation_id


@pytest.mark.parametrize("status", ["succeeded", "failed", "cancelled"])
def test_list_child_agents_projects_terminal_and_archived_state(
    store: Store, tmp_path: Path, status: str
) -> None:
    _, child_package = _setup(store, tmp_path / "workspace")
    delegation = _service(store).start_delegation(
        "operation-1", "step-1", "tool-1", UserMessage((TextBlock("child"),))
    )
    operation_id = _accept_child_operation(
        store,
        delegation.child_session_id,
        child_package.package_version_id,
        tmp_path / "workspace",
    )
    current = store.load_run_state(operation_id)
    assert current is not None
    terminal = replace(
        current,
        revision=2,
        status=status,
        waiting_reason=None,
        completed_step_count=3,
        final_assistant_node_id=("assistant-node" if status == "succeeded" else None),
        error=(AgentRunError("test", "failed", True) if status == "failed" else None),
        cancellation=(Cancellation("test", NOW) if status == "cancelled" else None),
    )
    if status == "succeeded" and not isinstance(store, InMemoryRuntimeStore):
        assistant = AssistantMessage(content=(TextBlock("done"),))
        with store._connect() as connection:
            connection.execute(
                "INSERT INTO conversation_nodes VALUES (?, ?, ?, ?, ?, ?)",
                (
                    "assistant-node",
                    delegation.child_session_id,
                    None,
                    "agent_message",
                    json.dumps(
                        agent_message_to_dict(assistant),
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    NOW.isoformat(),
                ),
            )
    _set_child_state(store, operation_id, terminal, active=False)
    _switch_parent_call_to_list_agents(store)

    service = DelegationService(store=store)
    snapshot = service.list_child_agents("operation-1", "step-1", "tool-list")[0]
    assert snapshot.status == status
    assert snapshot.operation_id == operation_id
    store.archive_session(session_id=delegation.child_session_id, archived_at=NOW)
    archived = service.list_child_agents("operation-1", "step-1", "tool-list")[0]
    assert archived.status == "archived"


def test_send_parent_followup_is_fifo_and_idempotent_after_claim(
    store: Store, tmp_path: Path
) -> None:
    _, child_package = _setup(store, tmp_path / "workspace")
    delegation = _service(store).start_delegation(
        "operation-1", "step-1", "tool-1", UserMessage((TextBlock("child"),))
    )
    _switch_parent_call_to_send_message(store, delegation.child_session_id)
    service = DelegationService(store=store, now=lambda: NOW)
    message = UserMessage((TextBlock("continue"),))

    first = service.send_parent_followup(
        "operation-1", "step-1", "tool-send", delegation.child_session_id, message
    )
    assert first.sequence == 2
    assert first.source == AgentMessageSource(
        sender_session_id="session-1",
        sender_operation_id="operation-1",
        form="followup",
    )
    child_operation = SessionOperation(
        operation_id="child-operation",
        session_id=delegation.child_session_id,
        agent_package_version_id=child_package.package_version_id,
        workspace_binding=WorkspaceBinding(
            "workspace-1", tmp_path / "workspace", tmp_path / "workspace"
        ),
        input_node_id=delegation.initial_message_id,
        accepted_at=NOW,
    )
    child_state = AgentRunState(
        operation_id="child-operation",
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
        operation=child_operation, state=child_state, expected_node_id=None
    )
    assert store.claim_message(
        message_id=first.message_id,
        operation_id="child-operation",
        step_id=None,
        handled_at=NOW,
    )
    retry = service.send_parent_followup(
        "operation-1", "step-1", "tool-send", delegation.child_session_id, message
    )
    assert retry.message_id == first.message_id
    assert retry == store.load_message(first.message_id)
    assert retry.status == "claimed"


def test_send_parent_followup_replays_existing_message_after_child_archive(
    store: Store, tmp_path: Path
) -> None:
    _setup(store, tmp_path / "workspace")
    delegation = _service(store).start_delegation(
        "operation-1", "step-1", "tool-1", UserMessage((TextBlock("child"),))
    )
    _switch_parent_call_to_send_message(store, delegation.child_session_id)
    service = DelegationService(store=store, now=lambda: NOW)
    message = UserMessage((TextBlock("continue"),))
    first = service.send_parent_followup(
        "operation-1", "step-1", "tool-send", delegation.child_session_id, message
    )
    assert store.discard_message(
        message_id=delegation.initial_message_id,
        reason="test cleanup",
        handled_at=NOW,
    )
    assert store.discard_message(
        message_id=first.message_id,
        reason="test cleanup",
        handled_at=NOW,
    )
    store.archive_session(session_id=delegation.child_session_id, archived_at=NOW)

    retry = service.send_parent_followup(
        "operation-1", "step-1", "tool-send", delegation.child_session_id, message
    )
    assert retry == store.load_message(first.message_id)
    assert retry.status == "discarded"

    _switch_parent_call_to_send_message(
        store,
        delegation.child_session_id,
        tool_call_id="tool-send-new",
        message_text="new",
    )
    with pytest.raises(StorageConflictError):
        service.send_parent_followup(
            "operation-1",
            "step-1",
            "tool-send-new",
            delegation.child_session_id,
            UserMessage((TextBlock("new"),)),
        )


def test_send_parent_followup_rejects_argument_or_target_mismatch(
    store: Store, tmp_path: Path
) -> None:
    _setup(store, tmp_path / "workspace")
    delegation = _service(store).start_delegation(
        "operation-1", "step-1", "tool-1", UserMessage((TextBlock("child"),))
    )
    _switch_parent_call_to_send_message(store, delegation.child_session_id)
    service = DelegationService(store=store, now=lambda: NOW)

    with pytest.raises(StorageConflictError):
        service.send_parent_followup(
            "operation-1",
            "step-1",
            "tool-send",
            delegation.child_session_id,
            UserMessage((TextBlock("different"),)),
        )
    with pytest.raises(StorageConflictError):
        service.send_parent_followup(
            "operation-1",
            "step-1",
            "tool-send",
            "not-a-child",
            UserMessage((TextBlock("continue"),)),
        )


def test_send_parent_followup_allows_a_later_operation_of_parent_session(
    store: Store, tmp_path: Path
) -> None:
    parent_package, _ = _setup(store, tmp_path / "workspace")
    delegation = _service(store).start_delegation(
        "operation-1", "step-1", "tool-1", UserMessage((TextBlock("child"),))
    )
    parent = store.load_session("session-1")
    assert parent is not None
    if isinstance(store, InMemoryRuntimeStore):
        store._sessions["session-1"] = replace(parent, active_operation_id=None)
    else:
        with store._connect() as connection:
            connection.execute(
                "UPDATE conversation_sessions SET active_operation_id = NULL WHERE session_id = ?",
                ("session-1",),
            )
    input_message = store.send_message(
        message_id="message-2",
        session_id="session-1",
        delivery="followup",
        message=UserMessage((TextBlock("next parent turn"),)),
        source=AgentMessageSource(
            sender_session_id="session-1",
            sender_operation_id="operation-2",
            form="followup",
        ),
        created_at=NOW,
    )
    operation = SessionOperation(
        operation_id="operation-2",
        session_id="session-1",
        agent_package_version_id=parent_package.package_version_id,
        workspace_binding=WorkspaceBinding(
            "workspace-1", tmp_path / "workspace", tmp_path / "workspace"
        ),
        input_node_id=input_message.message_id,
        accepted_at=NOW,
    )
    queued = AgentRunState(
        operation_id="operation-2",
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
        operation=operation,
        state=queued,
        expected_node_id=parent.active_node_id,
    )
    _switch_parent_call_to_send_message(
        store,
        delegation.child_session_id,
        operation_id="operation-2",
        step_id="step-2",
        tool_call_id="tool-send-2",
        message_text="later",
    )

    sent = DelegationService(store=store, now=lambda: NOW).send_parent_followup(
        "operation-2",
        "step-2",
        "tool-send-2",
        delegation.child_session_id,
        UserMessage((TextBlock("later"),)),
    )
    assert sent.source == AgentMessageSource(
        sender_session_id="session-1",
        sender_operation_id="operation-2",
        form="followup",
    )
    _switch_parent_call_to_list_agents(
        store, operation_id="operation-2", step_id="step-3", tool_call_id="tool-list-2"
    )
    snapshots = DelegationService(store=store).list_child_agents(
        "operation-2", "step-3", "tool-list-2"
    )
    assert [item.child_session_id for item in snapshots] == [
        delegation.child_session_id
    ]
