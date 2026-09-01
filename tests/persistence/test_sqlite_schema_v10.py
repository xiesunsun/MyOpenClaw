from __future__ import annotations

import sqlite3

import pytest

from pickel.persistence.sqlite_schema_v10 import (
    SCHEMA_VERSION,
    UnsupportedSchemaVersionError,
    create_schema,
    create_schema_objects,
)

TARGET_TABLES = {
    "workspaces",
    "conversation_sessions",
    "conversation_nodes",
    "agent_inbox_messages",
    "agent_package_versions",
    "session_operations",
    "agent_run_states",
    "artifacts",
    "agent_delegations",
}


def _connection() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.execute("PRAGMA foreign_keys = ON")
    create_schema(connection)
    return connection


def _seed_two_sessions(connection: sqlite3.Connection) -> None:
    connection.executemany(
        "INSERT INTO workspaces VALUES (?, ?, ?)",
        [("w1", "/tmp/w1", "2026-08-25T00:00:00Z")],
    )
    connection.execute(
        "INSERT INTO agent_package_versions VALUES (?, ?, ?, ?, ?)",
        ("package-1", "agent", 1, '{"agent_id":"agent"}', "now"),
    )
    connection.executemany(
        """
        INSERT INTO conversation_sessions (
            session_id, agent_id, workspace_id, cwd, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        [
            ("s1", "agent", "w1", "/tmp/w1", "now", "now"),
            ("s2", "agent", "w1", "/tmp/w1", "now", "now"),
        ],
    )
    connection.executemany(
        """
        INSERT INTO conversation_nodes (
            node_id, session_id, content_type, content_json, created_at
        ) VALUES (?, ?, 'agent_message', ?, 'now')
        """,
        [("n1", "s1", '{"role":"user"}'), ("n2", "s2", '{"role":"user"}')],
    )
    connection.execute("""
        INSERT INTO session_operations (
            operation_id, session_id, agent_package_version_id,
            workspace_binding_json, input_node_id, accepted_at
        ) VALUES ('op1', 's1', 'package-1', '{"workspace_id":"w1"}', 'n1', 'now')
        """)


def test_schema_is_v10_and_contains_only_target_tables() -> None:
    connection = _connection()

    tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }

    assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert tables == TARGET_TABLES
    assert not tables.intersection(
        {"sessions", "storage_commits", "immutable_objects", "named_references"}
    )
    assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_schema_creation_is_idempotent_and_rejects_v9() -> None:
    connection = _connection()
    create_schema(connection)
    assert connection.execute("PRAGMA user_version").fetchone()[0] == 10

    old = sqlite3.connect(":memory:")
    old.execute("PRAGMA user_version = 9")
    with pytest.raises(UnsupportedSchemaVersionError):
        create_schema(old)


def test_schema_objects_stays_inside_migration_transaction() -> None:
    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE TABLE legacy (id TEXT)")
    connection.commit()

    connection.execute("BEGIN")
    create_schema_objects(connection)
    connection.execute("INSERT INTO workspaces VALUES ('w', '/tmp/w', 'now')")
    assert connection.in_transaction
    assert connection.execute(
        "SELECT name FROM sqlite_master WHERE name = 'workspaces'"
    ).fetchone() == ("workspaces",)

    connection.rollback()
    assert (
        connection.execute(
            "SELECT name FROM sqlite_master WHERE name = 'workspaces'"
        ).fetchone()
        is None
    )
    assert connection.execute("PRAGMA user_version").fetchone()[0] == 0
    assert connection.execute(
        "SELECT name FROM sqlite_master WHERE name = 'legacy'"
    ).fetchone() == ("legacy",)


def test_expected_indexes_exist() -> None:
    connection = _connection()
    indexes = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index'"
        )
        if not row[0].startswith("sqlite_autoindex_")
    }

    assert {
        "idx_conversation_sessions_cwd_updated",
        "idx_conversation_nodes_session_parent",
        "idx_agent_inbox_messages_pending",
        "idx_agent_package_versions_agent_created",
        "idx_session_operations_session_accepted",
        "idx_agent_run_states_status",
        "idx_agent_delegations_parent",
    } <= indexes


def test_composite_foreign_keys_are_deferred_and_allow_atomic_acceptance() -> None:
    connection = _connection()

    connection.execute("BEGIN")
    connection.execute("""
        INSERT INTO workspaces VALUES ('w', '/tmp/atomic', 'now')
        """)
    connection.execute("""
        INSERT INTO conversation_sessions (
            session_id, agent_id, workspace_id, cwd, active_node_id,
            created_at, updated_at
        ) VALUES ('s', 'agent', 'w', '/tmp/atomic', 'n', 'now', 'now')
        """)
    connection.execute("""
        INSERT INTO conversation_nodes (
            node_id, session_id, content_type, content_json, created_at
        ) VALUES ('n', 's', 'agent_message', '{"role":"user"}', 'now')
        """)
    connection.commit()
    assert (
        connection.execute(
            "SELECT active_node_id FROM conversation_sessions WHERE session_id = 's'"
        ).fetchone()[0]
        == "n"
    )


@pytest.mark.parametrize(
    ("table", "column", "value"),
    [
        ("agent_package_versions", "content_json", "[]"),
        ("conversation_nodes", "content_json", "not-json"),
        ("session_operations", "workspace_binding_json", "[]"),
        ("agent_run_states", "current_step_json", "not-json"),
    ],
)
def test_json_columns_require_objects(table: str, column: str, value: str) -> None:
    connection = _connection()
    if table == "agent_package_versions":
        sql = f"INSERT INTO {table} VALUES ('id', 'agent', 1, ?, 'now')"
    elif table == "conversation_nodes":
        connection.execute("INSERT INTO workspaces VALUES ('w', '/tmp/j', 'now')")
        connection.execute(
            "INSERT INTO conversation_sessions (session_id, agent_id, workspace_id, cwd, created_at, updated_at) VALUES ('s', 'a', 'w', '/tmp/j', 'now', 'now')"
        )
        sql = f"INSERT INTO {table} (node_id, session_id, content_type, {column}, created_at) VALUES ('n', 's', 'agent_message', ?, 'now')"
    elif table == "session_operations":
        _seed_two_sessions(connection)
        sql = f"INSERT INTO {table} (operation_id, session_id, agent_package_version_id, workspace_binding_json, input_node_id, accepted_at) VALUES ('op2', 's1', 'package-1', ?, 'n1', 'now')"
    else:
        _seed_two_sessions(connection)
        sql = f"INSERT INTO {table} (operation_id, revision, status, current_step_json, updated_at) VALUES ('op1', 1, 'queued', ?, 'now')"

    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(sql, (value,))


def test_status_checks_require_matching_run_payloads() -> None:
    connection = _connection()
    _seed_two_sessions(connection)

    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO agent_run_states (operation_id, revision, status, updated_at) VALUES ('op1', 1, 'waiting', 'now')"
        )
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO agent_run_states (operation_id, revision, status, error_json, updated_at) VALUES ('op1', 1, 'running', '{}', 'now')"
        )


def test_claimed_inbox_message_may_not_have_a_step_yet() -> None:
    connection = _connection()
    _seed_two_sessions(connection)
    connection.execute("""
        INSERT INTO agent_inbox_messages (
            message_id, session_id, sequence, delivery, message_json, status,
            claimed_operation_id, created_at, handled_at
        ) VALUES ('m1', 's1', 1, 'followup', '{}', 'claimed', 'op1', 'now', 'now')
        """)
    connection.commit()


def test_inbox_sequence_starts_at_one() -> None:
    connection = _connection()
    _seed_two_sessions(connection)
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute("""
            INSERT INTO agent_inbox_messages (
                message_id, session_id, sequence, delivery, message_json,
                status, created_at
            ) VALUES ('m0', 's1', 0, 'followup', '{}', 'pending', 'now')
            """)


def test_cross_session_composite_references_are_rejected() -> None:
    connection = _connection()
    _seed_two_sessions(connection)
    connection.commit()

    connection.execute("BEGIN")
    connection.execute(
        "INSERT INTO conversation_sessions (session_id, agent_id, workspace_id, cwd, active_node_id, created_at, updated_at) VALUES ('s3', 'agent', 'w1', '/tmp/w1', 'n2', 'now', 'now')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        connection.commit()
    connection.rollback()

    connection.execute("BEGIN")
    connection.execute(
        "INSERT INTO session_operations (operation_id, session_id, agent_package_version_id, workspace_binding_json, input_node_id, accepted_at) VALUES ('op2', 's1', 'package-1', '{}', 'n2', 'now')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        connection.commit()
    connection.rollback()

    connection.execute("BEGIN")
    connection.execute(
        "INSERT INTO conversation_nodes (node_id, session_id, parent_node_id, content_type, content_json, created_at) VALUES ('n3', 's1', 'n2', 'agent_message', '{}', 'now')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        connection.commit()
    connection.rollback()


def test_inbox_and_delegation_cross_session_references_are_rejected() -> None:
    connection = _connection()
    _seed_two_sessions(connection)
    connection.execute(
        "INSERT INTO agent_inbox_messages (message_id, session_id, sequence, delivery, message_json, status, created_at) VALUES ('m2', 's2', 1, 'followup', '{}', 'pending', 'now')"
    )
    connection.commit()

    connection.execute("BEGIN")
    connection.execute(
        "UPDATE agent_inbox_messages SET status = 'claimed', claimed_operation_id = 'op1', claimed_step_id = 'step', handled_at = 'now' WHERE message_id = 'm2'"
    )
    with pytest.raises(sqlite3.IntegrityError):
        connection.commit()
    connection.rollback()

    connection.execute("BEGIN")
    connection.execute(
        "INSERT INTO agent_delegations (child_session_id, parent_operation_id, parent_step_id, parent_tool_call_id, initial_message_id, created_at) VALUES ('s1', 'op1', 'step', 'tool', 'm2', 'now')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        connection.commit()


def test_final_assistant_node_must_belong_to_operation_session() -> None:
    connection = _connection()
    _seed_two_sessions(connection)

    with pytest.raises(sqlite3.IntegrityError, match="final_assistant_node_id"):
        connection.execute(
            "INSERT INTO agent_run_states (operation_id, revision, status, final_assistant_node_id, updated_at) VALUES ('op1', 1, 'succeeded', 'n2', 'now')"
        )
