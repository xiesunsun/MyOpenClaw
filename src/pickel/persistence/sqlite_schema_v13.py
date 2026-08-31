"""SQLite v13 Runtime schema。

v13 与 v12 的表结构相同；ConversationNode 的 HistoryCompaction 内容由
一次性迁移改为自包含的 retained_messages 格式。
"""

from __future__ import annotations

import sqlite3

from pickel.persistence.sqlite_schema_v12 import (
    MODEL_CALLS_SQL,
    create_schema_objects as create_v12_schema_objects,
)

SCHEMA_VERSION = 13


class UnsupportedSchemaVersionError(RuntimeError):
    """数据库不是 v13，不能直接套用目标 schema。"""


def create_schema(connection: sqlite3.Connection) -> None:
    """在连接上创建 SQLite v13 schema，只接受空库或 v13。"""

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
    """在调用方事务内创建完整 v13 领域对象。"""

    create_v12_schema_objects(connection)


def initialize_schema(connection: sqlite3.Connection) -> None:
    create_schema(connection)


SCHEMA_V13_SQL = MODEL_CALLS_SQL

__all__ = [
    "MODEL_CALLS_SQL",
    "SCHEMA_V13_SQL",
    "SCHEMA_VERSION",
    "UnsupportedSchemaVersionError",
    "create_schema",
    "create_schema_objects",
    "initialize_schema",
]
