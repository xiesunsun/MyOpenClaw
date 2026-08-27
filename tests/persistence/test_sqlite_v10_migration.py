from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from pickel.persistence.sqlite_schema_v10 import create_schema as create_v10_schema
from pickel.persistence.sqlite_v10_migration import (
    SQLiteV10MigrationError,
    migrate_v10_to_v11,
)


def _seed_v10(path: Path, *, child_agent_id: str = "same-agent") -> None:
    connection = sqlite3.connect(path)
    create_v10_schema(connection)
    connection.execute(
        "INSERT INTO workspaces VALUES ('workspace', '/tmp/workspace', 'now')"
    )
    connection.execute(
        "INSERT INTO agent_package_versions VALUES (?, ?, ?, ?, ?)",
        ("parent-package", "same-agent", 1, "{}", "now"),
    )
    connection.executemany(
        """
        INSERT INTO conversation_sessions (
            session_id, agent_id, workspace_id, cwd, created_at, updated_at
        ) VALUES (?, ?, 'workspace', '/tmp/workspace', 'now', 'now')
        """,
        [("parent", "same-agent"), ("child", child_agent_id)],
    )
    connection.executemany(
        """
        INSERT INTO conversation_nodes (
            node_id, session_id, content_type, content_json, created_at
        ) VALUES (?, ?, 'agent_message', '{}', 'now')
        """,
        [("parent-node", "parent"), ("child-node", "child")],
    )
    connection.execute("""
        INSERT INTO session_operations (
            operation_id, session_id, agent_package_version_id,
            workspace_binding_json, input_node_id, accepted_at
        ) VALUES ('parent-operation', 'parent', 'parent-package', '{}', 'parent-node', 'now')
        """)
    connection.execute(
        "INSERT INTO agent_run_states (operation_id, revision, status, updated_at) VALUES ('parent-operation', 1, 'queued', 'now')"
    )
    connection.execute("""
        INSERT INTO agent_inbox_messages (
            message_id, session_id, sequence, delivery, message_json, status, created_at
        ) VALUES ('child-input', 'child', 1, 'followup', '{}', 'pending', 'now')
        """)
    connection.execute("""
        INSERT INTO agent_delegations (
            child_session_id, parent_operation_id, parent_step_id,
            parent_tool_call_id, initial_message_id, created_at
        ) VALUES ('child', 'parent-operation', 'step', 'tool', 'child-input', 'now')
        """)
    connection.commit()
    connection.close()


def test_migrate_v10_to_v11_backfills_parent_package_without_settled_replay(
    tmp_path: Path,
) -> None:
    path = tmp_path / "runtime.db"
    _seed_v10(path)

    result = migrate_v10_to_v11(path)

    assert result.delegation_count == 1
    assert result.backup_path.exists()
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 11
        assert (
            connection.execute(
                "SELECT child_package_version_id FROM agent_delegations"
            ).fetchone()[0]
            == "parent-package"
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM agent_inbox_messages "
                "WHERE json_extract(message_json, '$.source.kind') = 'agent_settled'"
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'agent_delegations_v10'"
            ).fetchone()
            is None
        )


def test_migrate_v10_to_v11_rolls_back_on_child_agent_mismatch(
    tmp_path: Path,
) -> None:
    path = tmp_path / "runtime.db"
    _seed_v10(path, child_agent_id="different-agent")

    with pytest.raises(SQLiteV10MigrationError, match="agent_id"):
        migrate_v10_to_v11(path)

    assert Path(f"{path}.v10.bak").exists()
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 10
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'agent_delegations'"
            ).fetchone()
            is not None
        )
        assert "child_package_version_id" not in {
            row[1] for row in connection.execute("PRAGMA table_info(agent_delegations)")
        }
