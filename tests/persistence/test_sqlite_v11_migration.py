from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from pickel.persistence.sqlite_schema_v11 import create_schema as create_v11_schema
from pickel.persistence.sqlite_v11_migration import (
    SQLiteV11MigrationError,
    migrate_v11_to_v12,
)


def _create_v11_db(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        create_v11_schema(connection)
    finally:
        connection.close()


def _version(path: Path) -> int:
    connection = sqlite3.connect(path)
    try:
        return int(connection.execute("PRAGMA user_version").fetchone()[0])
    finally:
        connection.close()


def _table_exists(path: Path, name: str) -> bool:
    connection = sqlite3.connect(path)
    try:
        row = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (name,),
        ).fetchone()
        return row is not None
    finally:
        connection.close()


def test_v11_to_v12_migration_keeps_backup_and_adds_model_calls(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "runtime.db"
    _create_v11_db(db_path)

    result = migrate_v11_to_v12(db_path)

    assert _version(db_path) == 12
    assert _table_exists(db_path, "model_calls")
    assert result.backup_path.is_file()
    assert _version(result.backup_path) == 11
    assert not _table_exists(result.backup_path, "model_calls")


def test_v11_to_v12_migration_rolls_back_schema_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pickel.persistence import sqlite_v11_migration

    db_path = tmp_path / "runtime.db"
    _create_v11_db(db_path)

    def fail_after_write(connection: sqlite3.Connection) -> None:
        connection.execute("CREATE TABLE should_rollback (id TEXT)")
        raise RuntimeError("boom")

    monkeypatch.setattr(
        sqlite_v11_migration,
        "_create_v12_objects",
        fail_after_write,
    )

    with pytest.raises(SQLiteV11MigrationError):
        migrate_v11_to_v12(db_path)

    assert _version(db_path) == 11
    assert not _table_exists(db_path, "should_rollback")
    assert not _table_exists(db_path, "model_calls")


def test_v11_to_v12_migration_preserves_existing_schema(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "runtime.db"
    _create_v11_db(db_path)
    connection = sqlite3.connect(db_path)
    try:
        before = connection.execute("""
            SELECT type, name, COALESCE(sql, '')
            FROM sqlite_master
            WHERE name NOT LIKE 'sqlite_%'
            ORDER BY type, name
            """).fetchall()
    finally:
        connection.close()

    migrate_v11_to_v12(db_path)

    connection = sqlite3.connect(db_path)
    try:
        after = connection.execute("""
            SELECT type, name, COALESCE(sql, '')
            FROM sqlite_master
            WHERE name NOT LIKE 'sqlite_%' AND tbl_name != 'model_calls'
            ORDER BY type, name
            """).fetchall()
        assert after == before
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()
