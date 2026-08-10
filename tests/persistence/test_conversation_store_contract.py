from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from pickel.conversations.conversation_store import ConversationStore
from pickel.persistence.in_memory_runtime_store import InMemoryRuntimeStore
from pickel.persistence.sqlite_runtime_store import SQLiteRuntimeStore
from pickel.persistence.storage_transaction import StorageConflictError


@pytest.fixture(params=["memory", "sqlite"])
def store(request, tmp_path: Path) -> ConversationStore:
    factories: dict[str, Callable[[], ConversationStore]] = {
        "memory": InMemoryRuntimeStore,
        "sqlite": lambda: SQLiteRuntimeStore(tmp_path / "conversation.db"),
    }
    result = factories[request.param]()
    result.create_conversation_session(
        session_id="session-1",
        agent_id="Pickle",
        cwd="/project",
    )
    return result


def _append_message(
    store: ConversationStore,
    *,
    expected_commit_sequence: int,
    expected_reference_sequence: int | None,
    parent_node_id: str | None,
    text: str,
) -> str:
    transaction = store.begin_storage_transaction(
        session_id="session-1",
        expected_commit_sequence=expected_commit_sequence,
    )
    object_id = transaction.insert_immutable_object(
        object_type="agent_message",
        content={"role": "user", "content": [{"type": "text", "text": text}]},
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
    transaction.commit()
    return node_id


def test_store_contract_commits_and_reads_active_branch(
    store: ConversationStore,
) -> None:
    first = _append_message(
        store,
        expected_commit_sequence=0,
        expected_reference_sequence=None,
        parent_node_id=None,
        text="first",
    )
    _append_message(
        store,
        expected_commit_sequence=1,
        expected_reference_sequence=1,
        parent_node_id=first,
        text="second",
    )

    session = store.load_conversation_session("session-1")
    entries = store.list_active_branch_entries(session_id="session-1")

    assert session is not None
    assert session.current_commit_sequence == 2
    assert [entry.object.content["content"][0]["text"] for entry in entries] == [
        "first",
        "second",
    ]


def test_store_contract_rejects_stale_session_sequence(
    store: ConversationStore,
) -> None:
    stale = store.begin_storage_transaction(
        session_id="session-1",
        expected_commit_sequence=0,
    )
    stale.insert_immutable_object(
        object_type="agent_message",
        content={"role": "user", "content": []},
    )
    _append_message(
        store,
        expected_commit_sequence=0,
        expected_reference_sequence=None,
        parent_node_id=None,
        text="winner",
    )

    with pytest.raises(StorageConflictError, match="sequence 冲突"):
        stale.commit()


def test_store_contract_moves_reference_to_create_branch(
    store: ConversationStore,
) -> None:
    first = _append_message(
        store,
        expected_commit_sequence=0,
        expected_reference_sequence=None,
        parent_node_id=None,
        text="first",
    )
    _append_message(
        store,
        expected_commit_sequence=1,
        expected_reference_sequence=1,
        parent_node_id=first,
        text="discarded",
    )
    transaction = store.begin_storage_transaction(
        session_id="session-1",
        expected_commit_sequence=2,
    )
    transaction.move_named_reference(
        reference_name="conversation/active",
        target_kind="node",
        target_id=first,
        expected_current_commit_sequence=2,
    )
    transaction.commit()
    _append_message(
        store,
        expected_commit_sequence=3,
        expected_reference_sequence=3,
        parent_node_id=first,
        text="replacement",
    )

    entries = store.list_active_branch_entries(session_id="session-1")
    assert [entry.object.content["content"][0]["text"] for entry in entries] == [
        "first",
        "replacement",
    ]


def test_store_contract_deletes_all_session_facts(store: ConversationStore) -> None:
    _append_message(
        store,
        expected_commit_sequence=0,
        expected_reference_sequence=None,
        parent_node_id=None,
        text="delete me",
    )

    store.delete_conversation_session(session_id="session-1")

    assert store.load_conversation_session("session-1") is None
