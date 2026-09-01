"""一次性将 Runtime SQLite v10 库转换为 v11。

迁移只为 AgentDelegation 增加精确的 child Package 绑定。所有校验和表替换
在同一个事务中执行；失败时恢复原 v10 表形态，成功后不保留 v10 双写路径。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

V10 = 10
V11 = 11

_V10_TABLES = (
    "workspaces",
    "agent_package_versions",
    "conversation_sessions",
    "conversation_nodes",
    "agent_inbox_messages",
    "session_operations",
    "agent_run_states",
    "artifacts",
    "agent_delegations",
)


class SQLiteV10MigrationError(RuntimeError):
    """v10 → v11 迁移前置条件或引用校验失败。"""


@dataclass(frozen=True)
class SQLiteV10MigrationResult:
    backup_path: Path
    delegation_count: int


def migrate_v10_to_v11(
    db_path: Path,
    *,
    backup_path: Path | None = None,
) -> SQLiteV10MigrationResult:
    """原地迁移 v10 数据库，并返回保留的 v10 备份路径。"""

    db_path = Path(db_path)
    if not db_path.is_file():
        raise SQLiteV10MigrationError(f"v10 数据库不存在: {db_path}")
    backup = (
        Path(backup_path) if backup_path is not None else Path(f"{db_path}.v10.bak")
    )
    if backup == db_path:
        raise SQLiteV10MigrationError("v10 备份路径不能与数据库相同")
    if backup.exists():
        raise SQLiteV10MigrationError(f"v10 备份已存在，不覆盖已有备份: {backup}")
    backup.parent.mkdir(parents=True, exist_ok=True)

    connection = sqlite3.connect(db_path)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version != V10:
            raise SQLiteV10MigrationError(
                f"只支持从 SQLite schema v10 迁移，实际版本为 {version}"
            )
        _create_online_backup(connection, backup)
    finally:
        connection.close()

    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        connection.execute("BEGIN IMMEDIATE")
        _require_v10_tables(connection)
        connection.execute(
            "ALTER TABLE agent_delegations RENAME TO agent_delegations_v10"
        )
        connection.execute("DROP INDEX IF EXISTS idx_agent_delegations_parent")
        _create_v11_schema(connection)
        delegation_count = _copy_delegations(connection)
        connection.execute("DROP TABLE agent_delegations_v10")
        connection.execute("PRAGMA user_version = 11")
        connection.commit()
    except Exception as exc:
        connection.rollback()
        if isinstance(exc, SQLiteV10MigrationError):
            raise
        raise SQLiteV10MigrationError(f"SQLite v10 → v11 迁移失败: {exc}") from exc
    finally:
        connection.close()
    return SQLiteV10MigrationResult(backup, delegation_count)


def _create_online_backup(source: sqlite3.Connection, backup_path: Path) -> None:
    destination = sqlite3.connect(backup_path)
    try:
        source.backup(destination)
        destination.commit()
    except Exception as exc:
        destination.rollback()
        destination.close()
        backup_path.unlink(missing_ok=True)
        raise SQLiteV10MigrationError(f"创建 v10 SQLite 一致备份失败: {exc}") from exc
    else:
        destination.close()


def _require_v10_tables(connection: sqlite3.Connection) -> None:
    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    ).fetchall()
    names = {str(row[0]) for row in rows}
    missing = [name for name in _V10_TABLES if name not in names]
    if missing:
        raise SQLiteV10MigrationError(f"v10 数据库缺少表: {', '.join(missing)}")


def _create_v11_schema(connection: sqlite3.Connection) -> None:
    from pickel.persistence import sqlite_schema_v11

    sqlite_schema_v11.create_schema_objects(connection)


def _copy_delegations(connection: sqlite3.Connection) -> int:
    rows = connection.execute("""
        SELECT d.child_session_id, d.parent_operation_id, d.parent_step_id,
               d.parent_tool_call_id, d.initial_message_id, d.created_at,
               operation.agent_package_version_id AS child_package_version_id,
               child.agent_id AS child_agent_id
        FROM agent_delegations_v10 AS d
        LEFT JOIN session_operations AS operation
          ON operation.operation_id = d.parent_operation_id
        LEFT JOIN conversation_sessions AS child
          ON child.session_id = d.child_session_id
        ORDER BY d.child_session_id
        """).fetchall()
    for row in rows:
        package_id = row["child_package_version_id"]
        if package_id is None:
            raise SQLiteV10MigrationError(
                "AgentDelegation 的 parent Operation 或 child Session 不存在: "
                f"child_session_id={row['child_session_id']}"
            )
        package = connection.execute(
            "SELECT agent_id FROM agent_package_versions WHERE package_version_id = ?",
            (package_id,),
        ).fetchone()
        if package is None:
            raise SQLiteV10MigrationError(
                "AgentDelegation 的 parent Operation Package 不存在: "
                f"package_version_id={package_id}"
            )
        if str(package["agent_id"]) != str(row["child_agent_id"]):
            raise SQLiteV10MigrationError(
                "AgentDelegation 的回填 Package agent_id 与 child Session 不一致: "
                f"child_session_id={row['child_session_id']}"
            )
        connection.execute(
            """
            INSERT INTO agent_delegations (
                child_session_id, child_package_version_id,
                parent_operation_id, parent_step_id,
                parent_tool_call_id, initial_message_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["child_session_id"],
                package_id,
                row["parent_operation_id"],
                row["parent_step_id"],
                row["parent_tool_call_id"],
                row["initial_message_id"],
                row["created_at"],
            ),
        )
    return len(rows)


migrate = migrate_v10_to_v11

__all__ = [
    "SQLiteV10MigrationError",
    "SQLiteV10MigrationResult",
    "V10",
    "V11",
    "migrate",
    "migrate_v10_to_v11",
]
