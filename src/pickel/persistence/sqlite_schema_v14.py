"""SQLite v14 Runtime schema，增加 AgentRunState.active_plan。"""

from __future__ import annotations

import sqlite3

from pickel.persistence.sqlite_schema_v13 import (
    create_schema_objects as create_v13_schema_objects,
)

SCHEMA_VERSION = 14
SCHEMA_V14_SQL = "ALTER TABLE agent_run_states ADD COLUMN active_plan_json TEXT NULL"


class UnsupportedSchemaVersionError(RuntimeError):
    """数据库不是 v14，不能直接套用目标 schema。"""


def create_schema(connection: sqlite3.Connection) -> None:
    connection.execute("PRAGMA foreign_keys = ON")
    version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if version not in (0, SCHEMA_VERSION):
        raise UnsupportedSchemaVersionError(
            f"不支持直接创建 SQLite schema version {version}；需要 0 或 {SCHEMA_VERSION}"
        )
    owns_transaction = not connection.in_transaction
    if owns_transaction:
        connection.execute("BEGIN")
    try:
        create_schema_objects(connection)
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    except Exception:
        if owns_transaction:
            connection.rollback()
        raise
    else:
        if owns_transaction:
            connection.commit()


def create_schema_objects(connection: sqlite3.Connection) -> None:
    """在调用方事务内创建完整 v14 领域对象。"""
    create_v13_schema_objects(connection)
    columns = {
        row[1]
        for row in connection.execute("PRAGMA table_info(agent_run_states)").fetchall()
    }
    if "active_plan_json" not in columns:
        connection.execute(
            "ALTER TABLE agent_run_states ADD COLUMN active_plan_json TEXT NULL "
            "CHECK (active_plan_json IS NULL OR "
            "(json_valid(active_plan_json) AND json_type(active_plan_json) = 'object'))"
        )
    # 共享 ModelCall mixin 的状态更新 SQL 不需要知道新列；终态统一清空计划。
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


def initialize_schema(connection: sqlite3.Connection) -> None:
    create_schema(connection)


__all__ = [
    "SCHEMA_V14_SQL",
    "SCHEMA_VERSION",
    "UnsupportedSchemaVersionError",
    "create_schema",
    "create_schema_objects",
    "initialize_schema",
]
