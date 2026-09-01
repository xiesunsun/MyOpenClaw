from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from pickel.persistence.sqlite_schema_v12 import create_schema
from pickel.persistence.sqlite_v12_migration import (
    SQLiteV12MigrationError,
    migrate_v12_to_v13,
)


def _database(
    path: Path, *, first: str | None = "message-1", cross_session: bool = False
) -> None:
    connection = sqlite3.connect(path)
    create_schema(connection)
    connection.execute(
        "INSERT INTO workspaces VALUES ('workspace-1', ?, '2026-08-31')",
        (str(path.parent),),
    )
    connection.execute(
        "INSERT INTO conversation_sessions VALUES ('session-1', 'agent', 'workspace-1', ?, NULL, NULL, NULL, NULL, '2026-08-31', '2026-08-31', NULL)",
        (str(path.parent),),
    )
    connection.execute(
        "INSERT INTO conversation_nodes VALUES ('message-1', 'session-1', NULL, 'agent_message', ?, '2026-08-31')",
        (
            json.dumps(
                {
                    "payload_version": 3,
                    "role": "user",
                    "content": [{"type": "text", "text": "hello"}],
                }
            ),
        ),
    )
    session = "session-2" if cross_session else "session-1"
    if cross_session:
        connection.execute(
            "INSERT INTO conversation_sessions VALUES ('session-2', 'agent', 'workspace-1', ?, NULL, NULL, NULL, NULL, '2026-08-31', '2026-08-31', NULL)",
            (str(path.parent),),
        )
        connection.execute(
            "INSERT INTO conversation_nodes VALUES ('message-2', 'session-2', NULL, 'agent_message', ?, '2026-08-31')",
            (
                json.dumps(
                    {
                        "payload_version": 3,
                        "role": "user",
                        "content": [{"type": "text", "text": "other"}],
                    }
                ),
            ),
        )
    parent = "message-2" if cross_session else "message-1"
    content = {"summary": "old", "first_kept_node_id": first}
    connection.execute(
        "INSERT INTO conversation_nodes VALUES ('checkpoint-1', ?, ?, 'history_compaction', ?, '2026-08-31')",
        (session, parent, json.dumps(content)),
    )
    connection.commit()
    connection.close()


def _version(path: Path) -> int:
    with sqlite3.connect(path) as connection:
        return int(connection.execute("PRAGMA user_version").fetchone()[0])


def test_v12_to_v13_migration_embeds_retained_messages_and_keeps_backup(
    tmp_path: Path,
) -> None:
    path = tmp_path / "runtime.db"
    _database(path)

    result = migrate_v12_to_v13(path)

    assert result.checkpoint_count == 1
    assert _version(path) == 13
    with sqlite3.connect(path) as connection:
        value = json.loads(
            connection.execute(
                "SELECT content_json FROM conversation_nodes WHERE node_id = 'checkpoint-1'"
            ).fetchone()[0]
        )
    assert value == {
        "summary": "old",
        "retained_messages": [
            {
                "content": [{"text": "hello", "type": "text"}],
                "payload_version": 3,
                "role": "user",
            }
        ],
    }
    assert _version(result.backup_path) == 12


@pytest.mark.parametrize(
    "kwargs", [{"first": None}, {"first": "missing"}, {"cross_session": True}]
)
def test_v12_to_v13_bad_reference_rolls_back_and_keeps_v12_backup(
    tmp_path: Path, kwargs: dict[str, object]
) -> None:
    path = tmp_path / "runtime.db"
    _database(path, **kwargs)

    with pytest.raises(SQLiteV12MigrationError):
        migrate_v12_to_v13(path)

    with sqlite3.connect(path) as connection:
        assert _version(path) == 12
        content = json.loads(
            connection.execute(
                "SELECT content_json FROM conversation_nodes WHERE node_id = 'checkpoint-1'"
            ).fetchone()[0]
        )
    assert "first_kept_node_id" in content
    assert "retained_messages" not in content
    assert (tmp_path / "runtime.db.v12.bak").is_file()
