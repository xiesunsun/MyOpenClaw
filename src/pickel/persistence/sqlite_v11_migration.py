"""一次性将 Runtime SQLite v11 库转换为 v12。"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

V11 = 11
V12 = 12


class SQLiteV11MigrationError(RuntimeError):
    """v11 → v12 迁移前置条件或执行失败。"""


@dataclass(frozen=True)
class SQLiteV11MigrationResult:
    backup_path: Path


def migrate_v11_to_v12(
    db_path: Path,
    *,
    backup_path: Path | None = None,
) -> SQLiteV11MigrationResult:
    """原地迁移 v11 数据库，并保留一致的 v11 备份。"""

    db_path = Path(db_path)
    if not db_path.is_file():
        raise SQLiteV11MigrationError(f"v11 数据库不存在: {db_path}")
    backup = (
        Path(backup_path) if backup_path is not None else Path(f"{db_path}.v11.bak")
    )
    if backup == db_path:
        raise SQLiteV11MigrationError("v11 备份路径不能与数据库相同")
    if backup.exists():
        raise SQLiteV11MigrationError(f"v11 备份已存在，不覆盖已有备份: {backup}")
    backup.parent.mkdir(parents=True, exist_ok=True)

    connection = sqlite3.connect(db_path)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version != V11:
            raise SQLiteV11MigrationError(
                f"只支持从 SQLite schema v11 迁移，实际版本为 {version}"
            )
        _create_online_backup(connection, backup)
    finally:
        connection.close()

    connection = sqlite3.connect(db_path)
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        connection.execute("BEGIN IMMEDIATE")
        original_schema = _schema_objects(connection)
        _create_v12_objects(connection)
        _validate_migrated_database(connection, original_schema)
        connection.execute("PRAGMA user_version = 12")
        connection.commit()
    except Exception as exc:
        connection.rollback()
        if isinstance(exc, SQLiteV11MigrationError):
            raise
        raise SQLiteV11MigrationError(f"SQLite v11 → v12 迁移失败: {exc}") from exc
    finally:
        connection.close()
    return SQLiteV11MigrationResult(backup_path=backup)


def _create_online_backup(source: sqlite3.Connection, backup_path: Path) -> None:
    destination = sqlite3.connect(backup_path)
    try:
        source.backup(destination)
        destination.commit()
    except Exception as exc:
        destination.rollback()
        destination.close()
        backup_path.unlink(missing_ok=True)
        raise SQLiteV11MigrationError(f"创建 v11 SQLite 一致备份失败: {exc}") from exc
    else:
        destination.close()


def _create_v12_objects(connection: sqlite3.Connection) -> None:
    from pickel.persistence.sqlite_schema_v12 import (
        create_model_call_schema_objects,
    )

    create_model_call_schema_objects(connection)


def _schema_objects(connection: sqlite3.Connection) -> dict[tuple[str, str], str]:
    """返回迁移前用户对象定义，供迁移后不变性校验使用。"""
    rows = connection.execute("""
        SELECT type, name, COALESCE(sql, '')
        FROM sqlite_master
        WHERE name NOT LIKE 'sqlite_%'
          AND tbl_name != 'model_calls'
        ORDER BY type, name
        """).fetchall()
    return {(str(row[0]), str(row[1])): str(row[2]) for row in rows}


def _validate_migrated_database(
    connection: sqlite3.Connection,
    original_schema: dict[tuple[str, str], str],
) -> None:
    """在提交前确认旧对象未被改写且外键完整。"""
    current = _schema_objects(connection)
    if current != original_schema:
        raise SQLiteV11MigrationError("v11 既有表或索引定义在迁移中发生变化")
    violations = connection.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        raise SQLiteV11MigrationError(f"迁移后外键完整性检查失败: {violations[:3]}")
    if not connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'model_calls'"
    ).fetchone():
        raise SQLiteV11MigrationError("迁移后缺少 model_calls 表")


migrate = migrate_v11_to_v12

__all__ = [
    "SQLiteV11MigrationError",
    "SQLiteV11MigrationResult",
    "V11",
    "V12",
    "migrate",
    "migrate_v11_to_v12",
]
