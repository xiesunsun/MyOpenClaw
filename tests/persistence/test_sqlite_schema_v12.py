from __future__ import annotations

import sqlite3

import pytest

from pickel.persistence.sqlite_schema_v12 import (
    SCHEMA_VERSION,
    create_schema,
)


def test_create_schema_v12_contains_model_calls_and_unique_attempt_index() -> None:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    create_schema(connection)

    assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    columns = {
        row["name"]
        for row in connection.execute("PRAGMA table_info(model_calls)").fetchall()
    }
    assert {
        "model_call_id",
        "session_id",
        "operation_id",
        "step_id",
        "request_attempt",
        "request_content_ref",
        "response_content_ref",
    } <= columns
    indexes = {
        row["name"]
        for row in connection.execute("PRAGMA index_list(model_calls)").fetchall()
    }
    assert "uq_model_calls_operation_step_attempt" in indexes


def test_model_calls_reject_invalid_identity_and_state_combinations() -> None:
    connection = sqlite3.connect(":memory:")
    create_schema(connection)
    connection.execute("""
        INSERT INTO workspaces VALUES ('workspace-1', '/tmp/workspace-1', '2026-08-27')
        """)
    connection.execute("""
        INSERT INTO conversation_sessions (
            session_id, agent_id, workspace_id, cwd,
            active_node_id, active_operation_id, title, title_source,
            created_at, updated_at, archived_at
        ) VALUES (
            'session-1', 'agent-1', 'workspace-1', '/tmp/workspace-1',
            NULL, NULL, NULL, NULL, '2026-08-27', '2026-08-27', NULL
        )
        """)

    with pytest.raises(sqlite3.IntegrityError):
        connection.execute("""
            INSERT INTO model_calls (
                model_call_id, session_id, operation_id,
                step_id, step_sequence, request_attempt,
                model_role, purpose, provider, api_kind, endpoint,
                requested_model, status, request_content_ref,
                context_fingerprint, created_at
            ) VALUES (
                'call-1', 'session-1', NULL,
                NULL, NULL, 1,
                'primary', 'agent_step', 'test', 'test', '/generate',
                'model', 'prepared', 'ref',
                NULL, '2026-08-27'
            )
            """)

    with pytest.raises(sqlite3.IntegrityError):
        connection.execute("""
            INSERT INTO model_calls (
                model_call_id, session_id, request_attempt,
                model_role, purpose, provider, api_kind, endpoint,
                requested_model, status, request_content_ref,
                created_at, started_at
            ) VALUES (
                'call-2', 'session-1', 1,
                'utility', 'title', 'test', 'test', '/generate',
                'model', 'prepared', 'ref',
                '2026-08-27', '2026-08-27'
            )
            """)
