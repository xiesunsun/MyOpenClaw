"""一次性将 Runtime SQLite v13 库转换为 v14。"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

V13 = 13
V14 = 14


class SQLiteV13MigrationError(RuntimeError):
    """v13 → v14 迁移前置条件或执行失败。"""


@dataclass(frozen=True)
class SQLiteV13MigrationResult:
    backup_path: Path


def migrate_v13_to_v14(
    db_path: Path, *, backup_path: Path | None = None
) -> SQLiteV13MigrationResult:
    """原地增加 active_plan_json 列，并保留一致的 v13 备份。"""
    db_path = Path(db_path)
    if not db_path.is_file():
        raise SQLiteV13MigrationError(f"v13 数据库不存在: {db_path}")
    backup = (
        Path(backup_path) if backup_path is not None else Path(f"{db_path}.v13.bak")
    )
    if backup == db_path or backup.exists():
        raise SQLiteV13MigrationError("v13 备份路径无效或已存在，不覆盖已有备份")
    backup.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(db_path)
    try:
        version = int(source.execute("PRAGMA user_version").fetchone()[0])
        if version != V13:
            raise SQLiteV13MigrationError(
                f"只支持从 SQLite schema v13 迁移，实际版本为 {version}"
            )
        destination = sqlite3.connect(backup)
        try:
            source.backup(destination)
            destination.commit()
        finally:
            destination.close()
    except SQLiteV13MigrationError:
        backup.unlink(missing_ok=True)
        raise
    except Exception as exc:
        backup.unlink(missing_ok=True)
        raise SQLiteV13MigrationError(f"创建 v13 SQLite 备份失败: {exc}") from exc
    finally:
        source.close()

    connection = sqlite3.connect(db_path)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("BEGIN IMMEDIATE")
        columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(agent_run_states)"
            ).fetchall()
        }
        if "active_plan_json" not in columns:
            connection.execute(
                "ALTER TABLE agent_run_states ADD COLUMN active_plan_json TEXT NULL "
                "CHECK (active_plan_json IS NULL OR "
                "(json_valid(active_plan_json) AND json_type(active_plan_json) = 'object'))"
            )
        connection.execute("""
            CREATE TRIGGER IF NOT EXISTS trg_agent_run_state_reject_terminal_plan_insert
            BEFORE INSERT ON agent_run_states
            WHEN NEW.status IN ('succeeded', 'failed', 'cancelled')
                 AND NEW.active_plan_json IS NOT NULL
            BEGIN
                SELECT RAISE(ABORT, 'terminal AgentRunState cannot have active_plan');
            END
            """)
        connection.execute("""
            CREATE TRIGGER IF NOT EXISTS trg_agent_run_state_clear_terminal_plan
            AFTER UPDATE OF status ON agent_run_states
            WHEN NEW.status IN ('succeeded', 'failed', 'cancelled')
                 AND NEW.active_plan_json IS NOT NULL
            BEGIN
                UPDATE agent_run_states SET active_plan_json = NULL
                WHERE operation_id = NEW.operation_id;
            END
            """)
        connection.execute(f"PRAGMA user_version = {V14}")
        connection.commit()
    except Exception as exc:
        connection.rollback()
        raise SQLiteV13MigrationError(f"SQLite v13 → v14 迁移失败: {exc}") from exc
    finally:
        connection.close()
    return SQLiteV13MigrationResult(backup_path=backup)


migrate = migrate_v13_to_v14


__all__ = [
    "SQLiteV13MigrationError",
    "SQLiteV13MigrationResult",
    "V13",
    "V14",
    "migrate",
    "migrate_v13_to_v14",
]
