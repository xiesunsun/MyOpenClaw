from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from pickel.agents.agent_package import decode_legacy_agent_package
from pickel.persistence.sqlite_v9_migration import (
    SQLiteV9MigrationError,
    migrate_v9_to_v10,
)


def _v9_database(
    path: Path,
    *,
    object_type: str = "agent_message",
    session_status: str = "active",
    state_status: str = "succeeded",
    artifact: tuple[str, str] | None = None,
) -> None:
    connection = sqlite3.connect(path)
    connection.executescript("""
        PRAGMA user_version = 9;
        CREATE TABLE sessions (
            session_id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, cwd TEXT NOT NULL,
            current_commit_sequence INTEGER NOT NULL, created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL, status TEXT NOT NULL, title TEXT
        );
        CREATE TABLE agent_package_versions (
            package_version_id TEXT PRIMARY KEY, digest TEXT NOT NULL,
            agent_id TEXT NOT NULL, content_json TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE artifacts (
            artifact_id TEXT PRIMARY KEY, digest TEXT NOT NULL, media_type TEXT NOT NULL,
            size_bytes INTEGER NOT NULL, blob_key TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE session_operations (
            operation_id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
            operation_type TEXT NOT NULL, agent_package_version_id TEXT NOT NULL,
            accepted_commit_sequence INTEGER NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE agent_delegations (
            delegation_id TEXT PRIMARY KEY, parent_operation_id TEXT NOT NULL,
            parent_step_id TEXT NOT NULL, parent_tool_call_id TEXT,
            child_operation_id TEXT NOT NULL, child_session_id TEXT NOT NULL,
            created_commit_sequence INTEGER NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE storage_commits (
            session_id TEXT NOT NULL, commit_sequence INTEGER NOT NULL,
            commit_id TEXT NOT NULL, committed_at TEXT NOT NULL
        );
        CREATE TABLE immutable_objects (
            object_id TEXT PRIMARY KEY, object_type TEXT NOT NULL,
            schema_version INTEGER NOT NULL, digest TEXT NOT NULL,
            content_json TEXT NOT NULL, created_session_id TEXT NOT NULL,
            created_commit_sequence INTEGER NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE conversation_nodes (
            node_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, parent_node_id TEXT,
            object_id TEXT NOT NULL, created_commit_sequence INTEGER NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE named_references (
            session_id TEXT NOT NULL, reference_name TEXT NOT NULL,
            commit_sequence INTEGER NOT NULL, target_kind TEXT NOT NULL,
            target_id TEXT NOT NULL
        );
        """)
    content = {"role": "user", "text": "hello"}
    state = {
        "operation_id": "operation-1",
        "revision": 1,
        "status": state_status,
        "user_message_node_id": "node-1",
        "completed_step_ids": [],
        "final_assistant_node_id": "node-1",
    }
    connection.executemany(
        "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                "session-1",
                "agent-1",
                str(path.parent),
                1,
                "2026-08-25T00:00:00+00:00",
                "2026-08-25T00:00:00+00:00",
                session_status,
                None,
            )
        ],
    )
    connection.execute(
        "INSERT INTO agent_package_versions VALUES (?, ?, ?, ?, ?)",
        (
            "package-1",
            "digest",
            "agent-1",
            json.dumps(_legacy_package_content()),
            "2026-08-25T00:00:00+00:00",
        ),
    )
    if artifact is not None:
        connection.execute(
            "INSERT INTO artifacts VALUES (?, ?, ?, ?, ?, ?)",
            (
                artifact[0],
                artifact[1],
                "application/octet-stream",
                3,
                "blob-key",
                "2026-08-25T00:00:00+00:00",
            ),
        )
    connection.execute(
        "INSERT INTO storage_commits VALUES (?, ?, ?, ?)",
        ("session-1", 1, "commit-1", "2026-08-25T00:00:00+00:00"),
    )
    connection.execute(
        "INSERT INTO immutable_objects VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "object-1",
            object_type,
            1,
            "digest",
            json.dumps(content),
            "session-1",
            1,
            "2026-08-25T00:00:00+00:00",
        ),
    )
    connection.execute(
        "INSERT INTO immutable_objects VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "state-1",
            "session_operation_state",
            1,
            "digest",
            json.dumps(state),
            "session-1",
            1,
            "2026-08-25T00:00:00+00:00",
        ),
    )
    connection.execute(
        "INSERT INTO conversation_nodes VALUES (?, ?, ?, ?, ?, ?)",
        ("node-1", "session-1", None, "object-1", 1, "2026-08-25T00:00:00+00:00"),
    )
    connection.execute(
        "INSERT INTO named_references VALUES (?, ?, ?, ?, ?)",
        ("session-1", "conversation/active", 1, "node", "node-1"),
    )
    connection.execute(
        "INSERT INTO named_references VALUES (?, ?, ?, ?, ?)",
        ("session-1", "operation/operation-1/state", 1, "object", "state-1"),
    )
    connection.execute(
        "INSERT INTO session_operations VALUES (?, ?, ?, ?, ?, ?)",
        (
            "operation-1",
            "session-1",
            "agent_run",
            "package-1",
            1,
            "2026-08-25T00:00:00+00:00",
        ),
    )
    connection.commit()
    connection.close()


def _legacy_package_content() -> dict:
    return {
        "schema_version": 3,
        "agent_id": "agent-1",
        "definition": {"file_access_mode": "workspace"},
        "behavior_instruction": "Be helpful.",
        "model": {
            "provider": "anthropic",
            "model": "claude-test",
            "api_base": None,
            "temperature": None,
            "max_input_tokens": None,
            "max_output_tokens": 1024,
            "provider_options": {},
            "required_secrets": [],
        },
        "runtime": {"max_model_steps": 8, "context_turn_window": 5},
        "skills": [],
        "tools": [],
    }


def _add_v9_delegation(path: Path, *, parent_tool_call_id: str | None) -> None:
    connection = sqlite3.connect(path)
    connection.execute(
        "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "session-2",
            "agent-1",
            str(path.parent),
            1,
            "2026-08-25T00:00:00+00:00",
            "2026-08-25T00:00:00+00:00",
            "active",
            None,
        ),
    )
    connection.execute(
        "INSERT INTO storage_commits VALUES (?, ?, ?, ?)",
        ("session-2", 1, "commit-2", "2026-08-25T00:00:00+00:00"),
    )
    child_content = {"role": "user", "text": "delegated"}
    child_state = {
        "operation_id": "operation-2",
        "revision": 1,
        "status": "succeeded",
        "user_message_node_id": "node-2",
        "completed_step_ids": [],
        "final_assistant_node_id": "node-2",
    }
    connection.execute(
        "INSERT INTO immutable_objects VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "object-2",
            "agent_message",
            1,
            "digest",
            json.dumps(child_content),
            "session-2",
            1,
            "2026-08-25T00:00:00+00:00",
        ),
    )
    connection.execute(
        "INSERT INTO immutable_objects VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "state-2",
            "session_operation_state",
            1,
            "digest",
            json.dumps(child_state),
            "session-2",
            1,
            "2026-08-25T00:00:00+00:00",
        ),
    )
    connection.execute(
        "INSERT INTO conversation_nodes VALUES (?, ?, ?, ?, ?, ?)",
        ("node-2", "session-2", None, "object-2", 1, "2026-08-25T00:00:00+00:00"),
    )
    connection.executemany(
        "INSERT INTO named_references VALUES (?, ?, ?, ?, ?)",
        [
            ("session-2", "conversation/active", 1, "node", "node-2"),
            ("session-2", "operation/operation-2/state", 1, "object", "state-2"),
        ],
    )
    connection.execute(
        "INSERT INTO session_operations VALUES (?, ?, ?, ?, ?, ?)",
        (
            "operation-2",
            "session-2",
            "agent_run",
            "package-1",
            1,
            "2026-08-25T00:00:00+00:00",
        ),
    )
    connection.execute(
        "INSERT INTO agent_delegations VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "delegation-1",
            "operation-1",
            "step-1",
            parent_tool_call_id,
            "operation-2",
            "session-2",
            1,
            "2026-08-25T00:00:00+00:00",
        ),
    )
    connection.commit()
    connection.close()


def test_migrate_v9_to_v10_is_atomic_and_keeps_backup(tmp_path: Path) -> None:
    path = tmp_path / "runtime.db"
    _v9_database(path)

    result = migrate_v9_to_v10(path)

    assert result.backup_path == Path(f"{path}.v9.bak")
    assert result.backup_path.exists()
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 10
        assert (
            connection.execute("SELECT COUNT(*) FROM conversation_nodes").fetchone()[0]
            == 1
        )
        assert (
            connection.execute("SELECT status FROM agent_run_states").fetchone()[0]
            == "succeeded"
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE name = 'immutable_objects'"
            ).fetchone()[0]
            == 0
        )


def test_migrate_v9_to_v10_rolls_back_when_content_type_is_unknown(
    tmp_path: Path,
) -> None:
    path = tmp_path / "runtime.db"
    _v9_database(path, object_type="unknown_object")

    with pytest.raises(SQLiteV9MigrationError, match="无法将旧 Object 类型"):
        migrate_v9_to_v10(path)

    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 9
        assert (
            connection.execute("SELECT COUNT(*) FROM immutable_objects").fetchone()[0]
            == 2
        )
    assert Path(f"{path}.v9.bak").exists()


def test_migration_rekeys_package_and_validates_terminal_state(tmp_path: Path) -> None:
    path = tmp_path / "runtime.db"
    _v9_database(path)

    migrate_v9_to_v10(path)

    package_id = decode_legacy_agent_package(
        content=_legacy_package_content(),
        created_at=datetime(2026, 8, 25, tzinfo=timezone.utc),
    ).package_version_id
    with sqlite3.connect(path) as connection:
        assert (
            connection.execute(
                "SELECT package_version_id FROM agent_package_versions"
            ).fetchone()[0]
            == package_id
        )
        assert (
            connection.execute(
                "SELECT agent_package_version_id FROM session_operations"
            ).fetchone()[0]
            == package_id
        )
        state = connection.execute(
            "SELECT status, current_step_json, error_json, cancellation_json "
            "FROM agent_run_states"
        ).fetchone()
    assert tuple(state) == ("succeeded", None, None, None)


@pytest.mark.parametrize("payload_version", [1, 2])
def test_old_agent_message_payload_is_normalized(
    tmp_path: Path, payload_version: int
) -> None:
    path = tmp_path / f"runtime-{payload_version}.db"
    _v9_database(path)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE immutable_objects SET content_json = ? WHERE object_id = ?",
            (
                json.dumps(
                    {
                        "payload_version": payload_version,
                        "role": "user",
                        "content": [{"type": "text", "text": "legacy"}],
                    }
                ),
                "object-1",
            ),
        )
        connection.commit()

    migrate_v9_to_v10(path)

    with sqlite3.connect(path) as connection:
        content = json.loads(
            connection.execute(
                "SELECT content_json FROM conversation_nodes"
            ).fetchone()[0]
        )
    assert content == {
        "payload_version": 3,
        "role": "user",
        "content": [{"type": "text", "text": "legacy"}],
    }


def test_old_artifact_reference_is_sanitized_and_verified(tmp_path: Path) -> None:
    path = tmp_path / "runtime.db"
    digest = "c" * 64
    artifact_id = f"artifact_{digest}"
    _v9_database(path, artifact=(artifact_id, digest))
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE immutable_objects SET content_json = ? WHERE object_id = ?",
            (
                json.dumps(
                    {
                        "payload_version": 1,
                        "role": "user",
                        "content": [
                            {
                                "type": "artifact",
                                "artifact": {
                                    "artifact_id": artifact_id,
                                    "digest": digest,
                                    "size_bytes": 3,
                                    "blob_key": "legacy-key",
                                    "media_type": "image/png",
                                    "display_name": "photo",
                                },
                            }
                        ],
                    }
                ),
                "object-1",
            ),
        )
        connection.commit()

    migrate_v9_to_v10(path)

    with sqlite3.connect(path) as connection:
        content = json.loads(
            connection.execute(
                "SELECT content_json FROM conversation_nodes"
            ).fetchone()[0]
        )
    assert content["content"][0]["artifact"] == {
        "artifact_id": artifact_id,
        "media_type": "image/png",
        "display_name": "photo",
    }


def test_history_compaction_drops_details_and_diagnostics(tmp_path: Path) -> None:
    path = tmp_path / "runtime.db"
    _v9_database(path)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE immutable_objects SET object_type = ?, content_json = ? "
            "WHERE object_id = ?",
            (
                "history_compaction",
                json.dumps(
                    {
                        "summary": "old summary",
                        "first_kept_node_id": None,
                        "details": {"diagnostic": "drop"},
                        "provider_usage": {"input_tokens": 42},
                    }
                ),
                "object-1",
            ),
        )
        connection.commit()

    migrate_v9_to_v10(path)

    with sqlite3.connect(path) as connection:
        content = json.loads(
            connection.execute(
                "SELECT content_json FROM conversation_nodes"
            ).fetchone()[0]
        )
    assert content == {"summary": "old summary", "first_kept_node_id": None}


def test_nonterminal_operation_becomes_stable_retryable_failed(tmp_path: Path) -> None:
    path = tmp_path / "runtime.db"
    _v9_database(path, state_status="running")

    migrate_v9_to_v10(path)

    with sqlite3.connect(path) as connection:
        state = connection.execute(
            "SELECT status, current_step_json, error_json FROM agent_run_states"
        ).fetchone()
    error = json.loads(state[2])
    assert tuple(state[:2]) == ("failed", None)
    assert error == {
        "code": "v9_migration_unrecoverable_operation",
        "message": "v9 Operation 缺少可恢复的冻结状态，迁移后不重放",
        "retryable": True,
    }


def test_archived_session_warning_and_artifact_identity(tmp_path: Path) -> None:
    path = tmp_path / "runtime.db"
    digest = "a" * 64
    _v9_database(
        path,
        session_status="archived",
        artifact=(f"artifact_{digest}", digest),
    )

    result = migrate_v9_to_v10(path)

    assert result.warnings == ("Session session-1 使用 v9 updated_at 作为 archived_at",)
    with sqlite3.connect(path) as connection:
        assert (
            connection.execute(
                "SELECT archived_at FROM conversation_sessions"
            ).fetchone()[0]
            == "2026-08-25T00:00:00+00:00"
        )
        assert connection.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 1


def test_bad_artifact_identity_rolls_back(tmp_path: Path) -> None:
    path = tmp_path / "runtime.db"
    _v9_database(path, artifact=("artifact_wrong", "b" * 64))

    with pytest.raises(SQLiteV9MigrationError, match="Artifact ID 与 digest"):
        migrate_v9_to_v10(path)

    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 9
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE name = 'immutable_objects'"
            ).fetchone()[0]
            == 1
        )


def test_missing_historical_cwd_is_normalized_without_existence_requirement(
    tmp_path: Path,
) -> None:
    path = tmp_path / "runtime.db"
    _v9_database(path)
    missing_cwd = tmp_path / "removed-project"
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE sessions SET cwd = ?", (str(missing_cwd),))
        connection.commit()

    migrate_v9_to_v10(path)

    normalized = str(missing_cwd.resolve())
    with sqlite3.connect(path) as connection:
        binding = json.loads(
            connection.execute(
                "SELECT workspace_binding_json FROM session_operations"
            ).fetchone()[0]
        )
        workspace = connection.execute("SELECT root_path FROM workspaces").fetchone()[0]
    assert workspace == normalized
    assert binding["working_directory"] == normalized
    assert binding["allowed_root"] == normalized


def test_existing_backup_is_never_overwritten(tmp_path: Path) -> None:
    path = tmp_path / "runtime.db"
    _v9_database(path)
    backup = Path(f"{path}.v9.bak")
    backup.write_bytes(b"sentinel")

    with pytest.raises(SQLiteV9MigrationError, match="备份已存在"):
        migrate_v9_to_v10(path)

    assert backup.read_bytes() == b"sentinel"
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 9


def test_online_backup_includes_uncheckpointed_wal_pages(tmp_path: Path) -> None:
    path = tmp_path / "runtime.db"
    _v9_database(path)
    writer = sqlite3.connect(path)
    try:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        writer.execute(
            "INSERT INTO immutable_objects VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "wal-object",
                "agent_message",
                1,
                "digest",
                json.dumps({"role": "user", "text": "from wal"}),
                "session-1",
                1,
                "2026-08-25T00:00:00+00:00",
            ),
        )
        writer.commit()
        assert Path(f"{path}-wal").exists()

        migrate_v9_to_v10(path)
    finally:
        writer.close()

    backup = Path(f"{path}.v9.bak")
    with sqlite3.connect(backup) as connection:
        assert connection.execute(
            "SELECT content_json FROM immutable_objects WHERE object_id = 'wal-object'"
        ).fetchone()[0] == json.dumps({"role": "user", "text": "from wal"})


def test_delegation_initial_message_is_migrated_from_child_input_node(
    tmp_path: Path,
) -> None:
    path = tmp_path / "runtime.db"
    _v9_database(path)
    _add_v9_delegation(path, parent_tool_call_id="tool-call-1")

    migrate_v9_to_v10(path)

    with sqlite3.connect(path) as connection:
        delegation = connection.execute(
            "SELECT child_session_id, initial_message_id FROM agent_delegations"
        ).fetchone()
        message = connection.execute(
            "SELECT status, claimed_operation_id, message_json "
            "FROM agent_inbox_messages"
        ).fetchone()
    assert tuple(delegation) == ("session-2", "node-2")
    assert tuple(message[:2]) == ("claimed", "operation-2")
    assert json.loads(message[2]) == {
        "message": {
            "content": [{"text": "delegated", "type": "text"}],
            "payload_version": 3,
            "role": "user",
        },
        "source": {
            "form": "followup",
            "kind": "agent",
            "sender_operation_id": "operation-1",
            "sender_session_id": "session-1",
        },
    }


def test_delegation_without_unique_tool_call_rolls_back(
    tmp_path: Path,
) -> None:
    path = tmp_path / "runtime.db"
    _v9_database(path)
    _add_v9_delegation(path, parent_tool_call_id=None)

    with pytest.raises(SQLiteV9MigrationError, match="parent_tool_call_id"):
        migrate_v9_to_v10(path)

    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 9
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE name = 'agent_delegations'"
            ).fetchone()[0]
            == 1
        )


def test_delegation_non_user_input_rolls_back(tmp_path: Path) -> None:
    path = tmp_path / "runtime.db"
    _v9_database(path)
    _add_v9_delegation(path, parent_tool_call_id="tool-call-1")
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE immutable_objects SET content_json = ? WHERE object_id = ?",
            (json.dumps({"role": "assistant", "text": "not user"}), "object-2"),
        )
        connection.commit()

    with pytest.raises(SQLiteV9MigrationError, match="AgentMessage.content"):
        migrate_v9_to_v10(path)

    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 9
