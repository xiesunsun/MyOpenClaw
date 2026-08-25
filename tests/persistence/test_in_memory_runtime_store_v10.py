from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

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
from pickel.conversations.agent_message import UserMessage
from pickel.conversations.content_blocks import ArtifactBlock, TextBlock
from pickel.conversations.conversation_node import ConversationNode
from pickel.conversations.conversation_session import ConversationSession
from pickel.inbox.message import InboxMessage, UserMessageSource
from pickel.operations.agent_run_state import AgentRunError, AgentRunState, Cancellation
from pickel.operations.session_operation import SessionOperation
from pickel.persistence.in_memory_runtime_store import InMemoryRuntimeStore
from pickel.persistence.errors import StorageIntegrityError
from pickel.workspaces.workspace import Workspace
from pickel.workspaces.workspace_binding import WorkspaceBinding

NOW = datetime(2026, 8, 25, tzinfo=timezone.utc)


def _package() -> AgentPackageVersion:
    return build_agent_package_version(
        agent_id="agent_1",
        format_version=1,
        behavior_instruction="be useful",
        model_policy=ModelPolicy(
            primary=ModelVersion(
                provider="test",
                model="test-model",
                api_base=None,
                temperature=None,
                max_input_tokens=None,
                max_output_tokens=100,
                provider_options={},
                provider_implementation=ImplementationRef("provider", "test"),
                required_secret_refs=(),
            )
        ),
        runtime_policy=AgentRuntimePolicy(max_model_steps=3, context_turn_window=10),
        workspace_policy=WorkspacePolicy("workspace"),
        skills=(),
        tools=(),
        extensions=(),
        created_at=NOW,
    )


def _store(tmp_path: Path) -> InMemoryRuntimeStore:
    store = InMemoryRuntimeStore()
    store.create_session(
        workspace=Workspace("workspace_1", tmp_path, NOW),
        session=ConversationSession(
            "session_1",
            "agent_1",
            "workspace_1",
            tmp_path,
            None,
            None,
            None,
            None,
            NOW,
            NOW,
            None,
        ),
    )
    store.insert_agent_package_version(_package())
    return store


def _message(message_id: str = "message_1") -> InboxMessage:
    return InboxMessage(
        message_id,
        "session_1",
        1,
        "followup",
        UserMessage(),
        UserMessageSource(),
        NOW,
    )


def _send(store: InMemoryRuntimeStore, message_id: str = "message_1") -> InboxMessage:
    value = _message(message_id)
    return store.send_message(
        message_id=message_id,
        session_id="session_1",
        delivery=value.delivery,
        message=value.message,
        source=value.source,
        created_at=NOW,
    )


def _operation(message_id: str = "message_1") -> tuple[SessionOperation, AgentRunState]:
    package = _package()
    operation = SessionOperation(
        "operation_1",
        "session_1",
        package.package_version_id,
        WorkspaceBinding("workspace_1", Path("/tmp"), None),
        message_id,
        NOW,
    )
    return operation, AgentRunState(
        operation_id="operation_1",
        revision=1,
        status="queued",
        waiting_reason=None,
        completed_step_count=0,
        current_step=None,
        final_assistant_node_id=None,
        error=None,
        cancellation=None,
    )


def test_accept_operation_is_atomic_and_uses_message_as_input_node(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    _send(store)
    operation, state = _operation()

    assert store.accept_operation(
        operation=operation,
        state=state,
        expected_node_id=None,
    )
    assert store.load_node("message_1") is not None
    assert store.load_operation("operation_1") == operation
    assert store.load_run_state("operation_1") == state
    assert store.list_pending(session_id="session_1") == ()
    assert store.load_session("session_1").active_operation_id == "operation_1"


def test_list_runnable_session_ids_ignores_history_limit_and_filters_inbox(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    for session_id in ("idle-steer", "idle-inject"):
        root = tmp_path / session_id
        root.mkdir()
        now = NOW
        store.create_session(
            workspace=Workspace(f"workspace-{session_id}", root, now),
            session=ConversationSession(
                session_id,
                "agent_1",
                f"workspace-{session_id}",
                root,
                None,
                None,
                None,
                None,
                now,
                now,
                None,
            ),
        )
    store.send_message(
        message_id="followup",
        session_id="session_1",
        delivery="followup",
        message=UserMessage(),
        source=UserMessageSource(),
        created_at=NOW,
    )
    for message_id, session_id, delivery in (
        ("steer", "idle-steer", "steer"),
        ("inject", "idle-inject", "inject"),
    ):
        store.send_message(
            message_id=message_id,
            session_id=session_id,
            delivery=delivery,
            message=UserMessage(),
            source=UserMessageSource(),
            created_at=NOW,
        )
    assert store.list_sessions(limit=1)[0].session_id == "idle-inject"
    assert store.list_runnable_session_ids() == ("idle-steer", "session_1")

    # 先接受消息使 Session 进入 active，再直接改变状态作为 fixture，独立验证
    # 查询合同而不把状态机转换路径混入测试。
    operation, state = _operation("followup")
    assert store.accept_operation(
        operation=operation, state=state, expected_node_id=None
    )
    for status in ("queued", "running", "cancelling"):
        store._run_states[operation.operation_id] = replace(
            state,
            status=status,
            cancellation=(
                Cancellation("startup", NOW) if status == "cancelling" else None
            ),
        )
        assert store.list_runnable_session_ids() == ("idle-steer", "session_1")
    store._run_states[operation.operation_id] = replace(
        state, status="waiting", waiting_reason="tool_approval"
    )
    assert store.list_runnable_session_ids() == ("idle-steer",)


def test_accept_operation_stale_cas_leaves_everything_untouched(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _send(store)
    operation, state = _operation()

    assert not store.accept_operation(
        operation=operation,
        state=state,
        expected_node_id="stale-node",
    )
    assert store.load_node("message_1") is None
    assert store.load_operation("operation_1") is None
    assert store.load_run_state("operation_1") is None
    assert len(store.list_pending(session_id="session_1")) == 1


def test_archive_and_delete_require_preconditions(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _send(store)
    with pytest.raises(StorageIntegrityError, match="pending"):
        store.archive_session(session_id="session_1", archived_at=NOW)
    assert store.load_session("session_1").archived_at is None

    assert store.discard_message(message_id="message_1", reason="test", handled_at=NOW)
    store.archive_session(session_id="session_1", archived_at=NOW)
    later = datetime(2026, 8, 25, 1, tzinfo=timezone.utc)
    store.unarchive_session(session_id="session_1", updated_at=later)
    assert store.load_session("session_1").updated_at == later
    store.archive_session(session_id="session_1", archived_at=later)
    store.delete_session(session_id="session_1")
    assert store.load_session("session_1") is None


def test_run_state_cas_requires_active_operation_and_uses_explicit_time(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    _send(store)
    operation, state = _operation()
    assert store.accept_operation(
        operation=operation,
        state=state,
        expected_node_id=None,
    )
    running = replace(state, revision=2, status="running")
    later = datetime(2026, 8, 25, 2, tzinfo=timezone.utc)
    assert store.commit_run_transition(
        state=running, expected_revision=1, updated_at=later, node=None
    )
    assert store.load_session("session_1").updated_at == later
    assert (
        store.commit_run_transition(
            state=replace(running, revision=3),
            expected_revision=1,
            updated_at=NOW,
            node=None,
        )
        is False
    )
    assert store.load_run_state("operation_1") == running

    failed = replace(
        running,
        revision=3,
        status="failed",
        error=AgentRunError("test", "failed", retryable=False),
    )
    finished = datetime(2026, 8, 25, 3, tzinfo=timezone.utc)
    assert store.commit_run_transition(
        state=failed, expected_revision=2, updated_at=finished, node=None
    )
    assert store.load_session("session_1").active_operation_id is None
    assert store.load_session("session_1").updated_at == finished

    # active 指针不再指向该 Operation 时，CAS 失败且不得覆盖 State。
    assert (
        store.commit_run_transition(
            state=replace(failed, revision=4),
            expected_revision=3,
            updated_at=NOW,
            node=None,
        )
        is False
    )


def test_package_identity_is_content_addressed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    version = _package()
    with pytest.raises(ValueError, match="canonical Package"):
        AgentPackageVersion(
            package_version_id="agentpkg_" + "0" * 64,
            agent_id=version.agent_id,
            format_version=version.format_version,
            behavior_instruction=version.behavior_instruction,
            model_policy=version.model_policy,
            runtime_policy=version.runtime_policy,
            workspace_policy=version.workspace_policy,
            skills=version.skills,
            tools=version.tools,
            extensions=version.extensions,
            created_at=version.created_at,
        )


def test_create_workspace_requires_existing_directory(tmp_path: Path) -> None:
    store = InMemoryRuntimeStore()
    with pytest.raises(StorageIntegrityError, match="现有目录"):
        store.create_session(
            workspace=Workspace("workspace_1", tmp_path / "missing", NOW),
            session=ConversationSession(
                "session_1",
                "agent_1",
                "workspace_1",
                tmp_path / "missing",
                None,
                None,
                None,
                None,
                NOW,
                NOW,
                None,
            ),
        )


def test_failed_session_creation_does_not_leave_workspace(tmp_path: Path) -> None:
    store = InMemoryRuntimeStore()
    session = ConversationSession(
        "session_1",
        "agent_1",
        "workspace_1",
        tmp_path,
        None,
        None,
        None,
        None,
        NOW,
        NOW,
        None,
    )
    with pytest.raises(StorageIntegrityError, match="不匹配"):
        store.create_session(
            workspace=Workspace("different_workspace", tmp_path, NOW),
            session=session,
        )
    assert store.load_workspace("different_workspace") is None


def test_sessions_reuse_workspace_identity_and_first_created_at(
    tmp_path: Path,
) -> None:
    store = InMemoryRuntimeStore()
    workspace = Workspace("workspace_1", tmp_path, NOW)
    first = ConversationSession(
        "session_1",
        "agent_1",
        "workspace_1",
        tmp_path,
        None,
        None,
        None,
        None,
        NOW,
        NOW,
        None,
    )
    second_time = datetime(2026, 8, 26, tzinfo=timezone.utc)
    second = replace(
        first, session_id="session_2", created_at=second_time, updated_at=second_time
    )
    store.create_session(workspace=workspace, session=first)
    store.create_session(
        workspace=replace(workspace, created_at=second_time), session=second
    )
    assert store.load_workspace("workspace_1").created_at == NOW
    assert len(store.list_sessions()) == 2


def test_send_allocates_sequence_and_requires_artifact(tmp_path: Path) -> None:
    store = _store(tmp_path)
    artifact_id = "artifact_" + "0" * 64
    message = UserMessage(
        content=[ArtifactBlock(ArtifactReference(artifact_id, "text/plain"))]
    )
    with pytest.raises(StorageIntegrityError, match="Artifact 不存在"):
        store.send_message(
            message_id="message_1",
            session_id="session_1",
            delivery="followup",
            message=message,
            source=UserMessageSource(),
            created_at=NOW,
        )
    store.insert_artifact(Artifact(artifact_id, 1, NOW))
    first = store.send_message(
        message_id="message_1",
        session_id="session_1",
        delivery="followup",
        message=message,
        source=UserMessageSource(),
        created_at=NOW,
    )
    second = store.send_message(
        message_id="message_2",
        session_id="session_1",
        delivery="inject",
        message=UserMessage(),
        source=UserMessageSource(),
        created_at=NOW,
    )
    assert (first.sequence, second.sequence) == (1, 2)


def test_append_node_is_active_leaf_cas_and_atomic(tmp_path: Path) -> None:
    store = _store(tmp_path)
    root = ConversationNode(
        "node_1",
        "session_1",
        None,
        "agent_message",
        UserMessage([TextBlock("hello")]),
        NOW,
    )
    assert store.append_node(node=root, expected_node_id=None)
    stale = ConversationNode(
        "node_2",
        "session_1",
        "node_1",
        "agent_message",
        UserMessage([TextBlock("stale")]),
        NOW,
    )
    assert not store.append_node(node=stale, expected_node_id=None)
    assert store.load_node("node_2") is None
    assert store.load_session("session_1").active_node_id == "node_1"


def test_package_is_rebuilt_from_deep_copied_content(tmp_path: Path) -> None:
    store = _store(tmp_path)
    loaded = store.load_agent_package_version(_package().package_version_id)
    assert loaded is not None
    loaded.content_dict()["runtime_policy"]["max_model_steps"] = 999
    assert (
        store.load_agent_package_version(
            _package().package_version_id
        ).runtime_policy.max_model_steps
        == 3
    )
