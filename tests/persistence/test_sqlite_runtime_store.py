from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from pickel.persistence.sqlite_runtime_store import (
    SQLiteRuntimeStore,
    UnsupportedStorageSchemaError,
)
from pickel.persistence.storage_transaction import (
    StorageConflictError,
    StorageIntegrityError,
)


def _store(tmp_path: Path) -> SQLiteRuntimeStore:
    store = SQLiteRuntimeStore(tmp_path / "conversations.db")
    store.create_conversation_session(
        session_id="session-1",
        agent_id="Pickle",
        cwd="/tmp/project",
    )
    return store


def _append_text(
    store: SQLiteRuntimeStore,
    *,
    text: str,
    parent_node_id: str | None,
    expected_commit_sequence: int,
    expected_reference_sequence: int | None,
) -> tuple[str, str, int]:
    transaction = store.begin_storage_transaction(
        session_id="session-1",
        expected_commit_sequence=expected_commit_sequence,
    )
    object_id = transaction.insert_immutable_object(
        object_type="agent_message",
        schema_version=1,
        content={"role": "user", "text": text},
    )
    node_id = transaction.append_conversation_node(
        object_id=object_id,
        parent_node_id=parent_node_id,
    )
    transaction.move_named_reference(
        reference_name="conversation/active",
        target_kind="node",
        target_id=node_id,
        expected_current_commit_sequence=expected_reference_sequence,
    )
    commit = transaction.commit()
    return object_id, node_id, commit.commit_sequence


def test_transaction_commits_object_node_reference_with_shared_sequence(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)

    object_id, node_id, sequence = _append_text(
        store,
        text="hello",
        parent_node_id=None,
        expected_commit_sequence=0,
        expected_reference_sequence=None,
    )

    assert sequence == 1
    assert store.load_current_commit_sequence("session-1") == 1
    immutable_object = store.load_immutable_object(object_id)
    assert immutable_object is not None
    assert immutable_object.created_commit_sequence == 1
    assert len(immutable_object.digest) == 64
    reference = store.find_named_reference(
        session_id="session-1",
        reference_name="conversation/active",
    )
    assert reference is not None
    assert reference.commit_sequence == 1
    assert reference.target_id == node_id
    session = store.load_conversation_session("session-1")
    assert session is not None
    assert session.current_commit_sequence == 1
    assert session.active_node_id == node_id


def test_active_branch_follows_reference_and_parent_chain(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _, first_node_id, _ = _append_text(
        store,
        text="first",
        parent_node_id=None,
        expected_commit_sequence=0,
        expected_reference_sequence=None,
    )
    _, second_node_id, _ = _append_text(
        store,
        text="second",
        parent_node_id=first_node_id,
        expected_commit_sequence=1,
        expected_reference_sequence=1,
    )

    entries = store.list_active_branch_entries(session_id="session-1")

    assert [entry.object.content["text"] for entry in entries] == [
        "first",
        "second",
    ]
    assert entries[-1].node.node_id == second_node_id
    assert [entry.node.created_commit_sequence for entry in entries] == [1, 2]


def test_failed_transaction_rolls_back_facts_and_does_not_consume_sequence(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    transaction = store.begin_storage_transaction(
        session_id="session-1",
        expected_commit_sequence=0,
    )
    object_id = transaction.insert_immutable_object(
        object_type="agent_message",
        content={"role": "user", "text": "orphan"},
    )
    transaction.append_conversation_node(
        object_id="missing-object",
        parent_node_id=None,
    )

    with pytest.raises(StorageIntegrityError, match="不存在的 Object"):
        transaction.commit()

    assert store.load_current_commit_sequence("session-1") == 0
    assert store.load_immutable_object(object_id) is None
    assert store.list_active_branch_entries(session_id="session-1") == []


def test_empty_transaction_does_not_consume_sequence(tmp_path: Path) -> None:
    store = _store(tmp_path)
    transaction = store.begin_storage_transaction(
        session_id="session-1",
        expected_commit_sequence=0,
    )

    with pytest.raises(StorageIntegrityError, match="不能为空"):
        transaction.commit()

    assert store.load_current_commit_sequence("session-1") == 0


def test_session_commit_sequence_compare_and_swap_rejects_stale_transaction(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    stale = store.begin_storage_transaction(
        session_id="session-1",
        expected_commit_sequence=0,
    )
    stale.insert_immutable_object(
        object_type="agent_message",
        content={"role": "user", "text": "stale"},
    )
    _append_text(
        store,
        text="winner",
        parent_node_id=None,
        expected_commit_sequence=0,
        expected_reference_sequence=None,
    )

    with pytest.raises(StorageConflictError, match="commit_sequence 冲突"):
        stale.commit()

    assert store.load_current_commit_sequence("session-1") == 1


def test_named_reference_compare_and_swap_rejects_stale_pointer(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    object_id, node_id, _ = _append_text(
        store,
        text="first",
        parent_node_id=None,
        expected_commit_sequence=0,
        expected_reference_sequence=None,
    )
    unrelated = store.begin_storage_transaction(
        session_id="session-1",
        expected_commit_sequence=1,
    )
    unrelated.move_named_reference(
        reference_name="bookmark/review",
        target_kind="object",
        target_id=object_id,
        expected_current_commit_sequence=None,
    )
    unrelated.commit()

    stale_pointer = store.begin_storage_transaction(
        session_id="session-1",
        expected_commit_sequence=2,
    )
    stale_pointer.move_named_reference(
        reference_name="conversation/active",
        target_kind="node",
        target_id=node_id,
        expected_current_commit_sequence=None,
    )

    with pytest.raises(StorageConflictError, match="commit_sequence 冲突"):
        stale_pointer.commit()

    assert store.load_current_commit_sequence("session-1") == 2


def test_duplicate_object_id_rolls_back_commit(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first = store.begin_storage_transaction(
        session_id="session-1",
        expected_commit_sequence=0,
    )
    first.insert_immutable_object(
        object_id="object-1",
        object_type="agent_message",
        content={"text": "first"},
    )
    first.commit()

    duplicate = store.begin_storage_transaction(
        session_id="session-1",
        expected_commit_sequence=1,
    )
    duplicate.insert_immutable_object(
        object_id="object-1",
        object_type="agent_message",
        content={"text": "second"},
    )

    with pytest.raises(StorageIntegrityError, match="Object 写入失败"):
        duplicate.commit()

    assert store.load_current_commit_sequence("session-1") == 1


def test_schema_version_mismatch_fails_without_modifying_database(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "legacy.db"
    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA user_version = 3")
        connection.execute("CREATE TABLE legacy (id TEXT)")

    store = SQLiteRuntimeStore(db_path)
    with pytest.raises(UnsupportedStorageSchemaError, match="需要 7"):
        store.load_current_commit_sequence("session-1")

    with sqlite3.connect(db_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 3
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE name = 'legacy'"
        ).fetchone()


def test_list_archive_and_delete_conversation_sessions(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.create_conversation_session(
        session_id="session-2",
        agent_id="Reviewer",
        cwd="/tmp/other",
        created_at=datetime(2026, 8, 12, tzinfo=timezone.utc),
    )

    assert [
        session.session_id for session in store.list_conversation_sessions(limit=10)
    ] == ["session-2", "session-1"]
    assert [
        session.session_id
        for session in store.list_conversation_sessions(
            limit=10,
            cwd="/tmp/project",
        )
    ] == ["session-1"]

    archived_at = datetime(2026, 8, 13, tzinfo=timezone.utc)
    store.archive_conversation_session(
        session_id="session-1",
        archived_at=archived_at,
    )
    archived = store.load_conversation_session("session-1")
    assert archived is not None
    assert archived.status == "archived"
    assert archived.updated_at == archived_at

    store.delete_conversation_session(session_id="session-1")
    assert store.load_conversation_session("session-1") is None
    with pytest.raises(LookupError, match="session-1"):
        store.delete_conversation_session(session_id="session-1")
