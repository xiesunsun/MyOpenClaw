from __future__ import annotations

import sqlite3
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
from pickel.conversations.agent_message import UserMessage
from pickel.conversations.content_blocks import TextBlock
from pickel.conversations.content_blocks import ArtifactBlock
from pickel.conversations.conversation_node import ConversationNode
from pickel.conversations.conversation_session import ConversationSession
from pickel.inbox.message import InboxMessage, UserMessageSource
from pickel.operations.agent_run_state import (
    AgentRunError,
    AgentRunState,
    Cancellation,
)
from pickel.operations.session_operation import SessionOperation
from pickel.persistence.sqlite_runtime_store import (
    _LIST_BRANCH_NODES_SQL,
    SQLiteRuntimeStore,
    StorageIntegrityError,
    UnsupportedStorageSchemaError,
)
from pickel.artifacts.artifact import Artifact, ArtifactReference
from pickel.workspaces.workspace import Workspace
from pickel.workspaces.workspace_binding import WorkspaceBinding

UTC = timezone.utc


def _package() -> AgentPackageVersion:
    return build_agent_package_version(
        agent_id="agent-1",
        format_version=1,
        behavior_instruction="test",
        model_policy=ModelPolicy(
            primary=ModelVersion(
                provider="test",
                model="test",
                wire_protocol="test",
                api_base=None,
                temperature=None,
                max_input_tokens=None,
                max_output_tokens=1,
                provider_options={},
                provider_implementation=ImplementationRef("provider", "test"),
                required_secret_refs=(),
            )
        ),
        runtime_policy=AgentRuntimePolicy(1, 1),
        workspace_policy=WorkspacePolicy("workspace"),
        skills=(),
        tools=(),
        extensions=(),
        created_at=datetime(2026, 8, 25, tzinfo=UTC),
    )


def _session(store: SQLiteRuntimeStore, root: Path) -> None:
    now = datetime(2026, 8, 25, tzinfo=UTC)
    store.create_session(
        workspace=Workspace("workspace-1", root, now),
        session=ConversationSession(
            "session-1",
            "agent-1",
            "workspace-1",
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


def _message(message_id: str = "message-1") -> InboxMessage:
    now = datetime(2026, 8, 25, tzinfo=UTC)
    return InboxMessage(
        message_id,
        "session-1",
        1,
        "followup",
        UserMessage((TextBlock("hello"),)),
        UserMessageSource(),
        now,
    )


@pytest.mark.parametrize("node_count", (1_000, 10_000))
def test_list_branch_nodes_handles_long_branches_and_session_isolation(
    tmp_path: Path, node_count: int
) -> None:
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    _session(store, tmp_path)
    second_root = tmp_path / "second"
    second_root.mkdir()
    now = datetime(2026, 8, 25, tzinfo=UTC)
    store.create_session(
        workspace=Workspace("workspace-2", second_root, now),
        session=ConversationSession(
            "session-2",
            "agent-1",
            "workspace-2",
            second_root,
            None,
            None,
            None,
            None,
            now,
            now,
            None,
        ),
    )
    content_json = (
        '{"content":[{"text":"branch","type":"text"}],'
        '"payload_version":3,"role":"user"}'
    )
    rows = [
        (
            f"long-{index:05d}",
            "session-1",
            None if index == 0 else f"long-{index - 1:05d}",
            "agent_message",
            content_json,
            now.isoformat(),
        )
        for index in range(node_count)
    ]
    with sqlite3.connect(store.db_path) as connection:
        connection.executemany(
            "INSERT INTO conversation_nodes "
            "(node_id, session_id, parent_node_id, content_type, content_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            rows,
        )
        connection.execute(
            "INSERT INTO conversation_nodes "
            "(node_id, session_id, parent_node_id, content_type, content_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                "other-0",
                "session-2",
                None,
                "agent_message",
                content_json,
                now.isoformat(),
            ),
        )

    leaf_id = f"long-{node_count - 1:05d}"
    branch = store.list_branch_nodes("session-1", leaf_id)
    assert len(branch) == node_count
    assert branch[0].node_id == "long-00000"
    assert branch[-1].node_id == leaf_id
    assert [node.node_id for node in branch] == [
        f"long-{index:05d}" for index in range(node_count)
    ]
    assert {node.session_id for node in branch} == {"session-1"}
    assert [
        node.node_id for node in store.list_branch_nodes("session-2", "other-0")
    ] == ["other-0"]
    assert store.list_branch_nodes("session-2", leaf_id) == ()


def test_list_branch_nodes_query_uses_node_lookup_index(tmp_path: Path) -> None:
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    _session(store, tmp_path)
    with sqlite3.connect(store.db_path) as connection:
        details = [
            str(row[3]).lower()
            for row in connection.execute(
                "EXPLAIN QUERY PLAN " + _LIST_BRANCH_NODES_SQL,
                ("missing", "session-1", "session-1"),
            )
        ]
    assert any(
        "search parent" in detail
        and ("node_id" in detail or "primary key" in detail or "autoindex" in detail)
        for detail in details
    ), details
    assert not any("scan parent" in detail for detail in details), details


def test_store_creates_only_v11_and_rejects_v9_and_v10(tmp_path: Path) -> None:
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    _session(store, tmp_path)
    with sqlite3.connect(store.db_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 11
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert "sessions" not in tables
        assert "conversation_sessions" in tables

    old_path = tmp_path / "old.db"
    with sqlite3.connect(old_path) as connection:
        connection.execute("PRAGMA user_version = 9")
    with pytest.raises(UnsupportedStorageSchemaError, match="一次性"):
        SQLiteRuntimeStore(old_path).load_session("missing")

    v10_path = tmp_path / "v10.db"
    with sqlite3.connect(v10_path) as connection:
        connection.execute("PRAGMA user_version = 10")
    with pytest.raises(UnsupportedStorageSchemaError, match="v10→v11"):
        SQLiteRuntimeStore(v10_path).load_session("missing")


def test_list_runnable_session_ids_ignores_history_limit_and_filters_inbox(
    tmp_path: Path,
) -> None:
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    _session(store, tmp_path)
    now = datetime(2026, 8, 25, tzinfo=UTC)
    for session_id in ("idle-steer", "idle-inject"):
        root = tmp_path / session_id
        root.mkdir()
        store.create_session(
            workspace=Workspace(f"workspace-{session_id}", root, now),
            session=ConversationSession(
                session_id,
                "agent-1",
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
    for message_id, session_id, delivery in (
        ("followup", "session-1", "followup"),
        ("steer", "idle-steer", "steer"),
        ("inject", "idle-inject", "inject"),
    ):
        store.send_message(
            message_id=message_id,
            session_id=session_id,
            delivery=delivery,
            message=UserMessage(),
            source=UserMessageSource(),
            created_at=now,
        )
    assert store.list_runnable_session_ids() == ("idle-steer", "session-1")

    package = _package()
    store.insert_agent_package_version(package)
    operation = SessionOperation(
        "operation-runnable",
        "session-1",
        package.package_version_id,
        WorkspaceBinding("workspace-1", tmp_path, None),
        "followup",
        now,
    )
    queued = AgentRunState(
        "operation-runnable", 1, "queued", None, 0, None, None, None, None
    )
    assert store.accept_operation(
        operation=operation, state=queued, expected_node_id=None
    )
    for status in ("queued", "running", "cancelling"):
        with sqlite3.connect(store.db_path) as connection:
            connection.execute(
                """
                UPDATE agent_run_states
                SET status = ?, cancellation_json = ?
                WHERE operation_id = ?
                """,
                (
                    status,
                    (
                        '{"cause":"startup",' f'"requested_at":"{now.isoformat()}"}}'
                        if status == "cancelling"
                        else None
                    ),
                    operation.operation_id,
                ),
            )
        assert store.list_runnable_session_ids() == ("idle-steer", "session-1")
    with sqlite3.connect(store.db_path) as connection:
        connection.execute(
            """
            UPDATE agent_run_states
            SET status = 'waiting', waiting_reason = 'tool_approval',
                cancellation_json = NULL
            WHERE operation_id = ?
            """,
            (operation.operation_id,),
        )
    assert store.list_runnable_session_ids() == ("idle-steer",)


def test_workspace_create_requires_directory_but_load_does_not(tmp_path: Path) -> None:
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    missing = tmp_path / "gone"
    workspace = Workspace("w", missing, datetime.now(UTC))
    with pytest.raises(ValueError, match="根目录不存在"):
        store.create_session(
            workspace=workspace,
            session=ConversationSession(
                "s",
                "a",
                "w",
                missing,
                None,
                None,
                None,
                None,
                datetime.now(UTC),
                datetime.now(UTC),
                None,
            ),
        )

    missing.mkdir()
    store.create_session(
        workspace=workspace,
        session=ConversationSession(
            "s",
            "a",
            "w",
            missing,
            None,
            None,
            None,
            None,
            datetime.now(UTC),
            datetime.now(UTC),
            None,
        ),
    )
    missing.rmdir()
    loaded = store.load_workspace("w")
    assert loaded is not None
    assert loaded.root_path == missing.resolve()

    other_root = tmp_path / "other"
    other_root.mkdir()
    with pytest.raises(StorageIntegrityError):
        store.create_session(
            workspace=Workspace("w2", other_root, datetime.now(UTC)),
            session=ConversationSession(
                "s",
                "a",
                "w2",
                other_root,
                None,
                None,
                None,
                None,
                datetime.now(UTC),
                datetime.now(UTC),
                None,
            ),
        )
    assert store.load_workspace("w2") is None


def test_sessions_reuse_workspace_and_keep_first_created_at(tmp_path: Path) -> None:
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    first_workspace = Workspace(
        "workspace_shared", tmp_path, datetime(2026, 1, 1, tzinfo=UTC)
    )
    second_workspace = Workspace(
        "workspace_shared", tmp_path, datetime(2027, 1, 1, tzinfo=UTC)
    )
    for session_id, workspace, created_at in (
        ("session-a", first_workspace, datetime(2026, 1, 2, tzinfo=UTC)),
        ("session-b", second_workspace, datetime(2027, 1, 2, tzinfo=UTC)),
    ):
        store.create_session(
            workspace=workspace,
            session=ConversationSession(
                session_id,
                "agent-1",
                "workspace_shared",
                tmp_path,
                None,
                None,
                None,
                None,
                created_at,
                created_at,
                None,
            ),
        )
    assert len(store.list_sessions()) == 2
    assert store.load_workspace("workspace_shared").created_at == first_workspace.created_at  # type: ignore[union-attr]


def test_conversation_node_and_active_leaf_use_natural_cas(tmp_path: Path) -> None:
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    _session(store, tmp_path)
    now = datetime.now(UTC)
    node = ConversationNode(
        "node-1",
        "session-1",
        None,
        "agent_message",
        UserMessage((TextBlock("hello"),)),
        now,
    )
    assert store.append_node(node=node, expected_node_id=None)
    stale = ConversationNode(
        "stale-node",
        "session-1",
        None,
        "agent_message",
        UserMessage((TextBlock("stale"),)),
        now,
    )
    assert not store.append_node(node=stale, expected_node_id=None)
    assert store.load_node("stale-node") is None
    branch = ConversationNode(
        "branch-node",
        "session-1",
        "node-1",
        "agent_message",
        UserMessage((TextBlock("branch"),)),
        now,
    )
    assert store.append_node(node=branch, expected_node_id="node-1")
    assert store.load_node("node-1") == node


def test_node_artifact_reference_must_exist(tmp_path: Path) -> None:
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    _session(store, tmp_path)
    artifact_id = "artifact_" + "a" * 64
    node = ConversationNode(
        "node-artifact",
        "session-1",
        None,
        "agent_message",
        UserMessage((ArtifactBlock(ArtifactReference(artifact_id, "image/png")),)),
        datetime.now(UTC),
    )
    with pytest.raises(StorageIntegrityError, match="Artifact"):
        store.append_node(node=node, expected_node_id=None)
    with pytest.raises(StorageIntegrityError, match="Artifact"):
        store.send_message(
            message_id="artifact-message",
            session_id="session-1",
            delivery="followup",
            message=node.content,
            source=UserMessageSource(),
            created_at=node.created_at,
        )
    store.insert_artifact(Artifact(artifact_id, 1, datetime.now(UTC)))
    assert store.append_node(node=node, expected_node_id=None)


def test_send_message_allocates_session_sequence(tmp_path: Path) -> None:
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    _session(store, tmp_path)
    first = _message("m-1")
    second = _message("m-2")
    sent_first = store.send_message(
        message_id=first.message_id,
        session_id=first.session_id,
        delivery=first.delivery,
        message=first.message,
        source=first.source,
        created_at=first.created_at,
    )
    sent_second = store.send_message(
        message_id=second.message_id,
        session_id=second.session_id,
        delivery=second.delivery,
        message=second.message,
        source=second.source,
        created_at=second.created_at,
    )
    assert (sent_first.sequence, sent_second.sequence) == (1, 2)
    assert [item.sequence for item in store.list_pending(session_id="session-1")] == [
        1,
        2,
    ]


def test_accept_operation_is_one_transaction_and_terminal_update_clears_pointer(
    tmp_path: Path,
) -> None:
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    _session(store, tmp_path)
    store.send_message(
        message_id="message-1",
        session_id="session-1",
        delivery="followup",
        message=_message().message,
        source=_message().source,
        created_at=_message().created_at,
    )
    now = datetime.now(UTC)
    package = _package()
    store.insert_agent_package_version(package)
    operation = SessionOperation(
        "operation-1",
        "session-1",
        package.package_version_id,
        WorkspaceBinding("workspace-1", tmp_path, None),
        "message-1",
        now,
    )
    state = AgentRunState("operation-1", 1, "queued", None, 0, None, None, None, None)
    assert store.accept_operation(
        operation=operation,
        state=state,
        expected_node_id=None,
    )
    assert store.load_node("message-1") is not None
    assert store.load_message("message-1").status == "claimed"  # type: ignore[union-attr]
    assert store.load_session("session-1").active_operation_id == "operation-1"  # type: ignore[union-attr]
    with sqlite3.connect(store.db_path) as connection:
        row = connection.execute(
            "SELECT updated_at FROM agent_run_states WHERE operation_id = ?",
            (operation.operation_id,),
        ).fetchone()
    assert row == (operation.accepted_at.isoformat(),)

    running = AgentRunState(
        "operation-1", 2, "running", None, 0, None, None, None, None
    )
    assert store.commit_run_transition(
        state=running, expected_revision=1, updated_at=now, node=None
    )
    with pytest.raises(StorageIntegrityError, match="revision"):
        store.commit_run_transition(
            state=AgentRunState(
                "operation-1", 99, "running", None, 0, None, None, None, None
            ),
            expected_revision=2,
            updated_at=now,
            node=None,
        )
    assert not store.commit_run_transition(
        state=running, expected_revision=1, updated_at=now, node=None
    )
    succeeded = AgentRunState(
        "operation-1", 3, "succeeded", None, 1, None, "message-1", None, None
    )
    finished_at = datetime(2026, 8, 25, 1, 2, 3, tzinfo=UTC)
    assert store.commit_run_transition(
        state=succeeded, expected_revision=2, updated_at=finished_at, node=None
    )
    assert store.load_run_state("operation-1") == succeeded
    assert store.load_session("session-1").active_operation_id is None  # type: ignore[union-attr]
    with sqlite3.connect(store.db_path) as connection:
        row = connection.execute(
            "SELECT updated_at FROM agent_run_states WHERE operation_id = 'operation-1'"
        ).fetchone()
    assert row == (finished_at.isoformat(),)


@pytest.mark.parametrize("status", ["succeeded", "failed", "cancelled"])
def test_terminal_update_clears_active_operation_atomically(
    tmp_path: Path, status: str
) -> None:
    root = tmp_path / status
    root.mkdir()
    store = SQLiteRuntimeStore(root / "runtime.db")
    _session(store, root)
    message = _message()
    store.send_message(
        message_id=message.message_id,
        session_id=message.session_id,
        delivery=message.delivery,
        message=message.message,
        source=message.source,
        created_at=message.created_at,
    )
    package = _package()
    store.insert_agent_package_version(package)
    accepted_at = datetime(2026, 8, 25, 5, 0, 0, tzinfo=UTC)
    operation = SessionOperation(
        f"operation-{status}",
        "session-1",
        package.package_version_id,
        WorkspaceBinding("workspace-1", root, None),
        message.message_id,
        accepted_at,
    )
    queued = AgentRunState(
        operation.operation_id, 1, "queued", None, 0, None, None, None, None
    )
    assert store.accept_operation(
        operation=operation, state=queued, expected_node_id=None
    )
    terminal_at = datetime(2026, 8, 25, 5, 1, 0, tzinfo=UTC)
    terminal = AgentRunState(
        operation.operation_id,
        2,
        status,  # type: ignore[arg-type]
        None,
        0,
        None,
        message.message_id if status == "succeeded" else None,
        AgentRunError("test", "failed", True) if status == "failed" else None,
        Cancellation("test", terminal_at) if status == "cancelled" else None,
    )
    assert store.commit_run_transition(
        state=terminal, expected_revision=1, updated_at=terminal_at, node=None
    )
    assert store.load_run_state(operation.operation_id) == terminal
    assert store.load_session("session-1").active_operation_id is None  # type: ignore[union-attr]
    with sqlite3.connect(store.db_path) as connection:
        row = connection.execute(
            "SELECT updated_at FROM agent_run_states WHERE operation_id = ?",
            (operation.operation_id,),
        ).fetchone()
    assert row == (terminal_at.isoformat(),)


def test_archive_and_delete_require_explicit_preconditions(tmp_path: Path) -> None:
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    _session(store, tmp_path)
    store.send_message(
        message_id="message-1",
        session_id="session-1",
        delivery="followup",
        message=_message().message,
        source=_message().source,
        created_at=_message().created_at,
    )
    with pytest.raises(StorageIntegrityError, match="pending"):
        store.archive_session(session_id="session-1", archived_at=datetime.now(UTC))
    assert store.discard_message(
        message_id="message-1", reason="test", handled_at=datetime.now(UTC)
    )
    now = datetime.now(UTC)
    store.archive_session(session_id="session-1", archived_at=now)
    store.archive_session(
        session_id="session-1", archived_at=datetime(2030, 1, 1, tzinfo=UTC)
    )
    assert store.load_session("session-1").archived_at == now  # type: ignore[union-attr]
    unarchived_at = datetime(2027, 1, 1, tzinfo=UTC)
    store.unarchive_session(session_id="session-1", updated_at=unarchived_at)
    store.unarchive_session(
        session_id="session-1", updated_at=datetime(2030, 1, 1, tzinfo=UTC)
    )
    assert store.load_session("session-1").updated_at == unarchived_at  # type: ignore[union-attr]
    store.archive_session(session_id="session-1", archived_at=now)
    store.delete_session(session_id="session-1")
    assert store.load_session("session-1") is None
