"""SQLite v12 Runtime schema。

v12 在 v11 领域表之上增加可靠的 ModelCall 事实表。运行期只接受空库或 v12；
v11 数据库必须先显式执行一次性迁移。
"""

from __future__ import annotations

import sqlite3

from pickel.persistence.sqlite_schema_v11 import (
    create_schema_objects as create_v11_schema_objects,
)

SCHEMA_VERSION = 12


class UnsupportedSchemaVersionError(RuntimeError):
    """数据库不是 v12，不能直接套用目标 schema。"""


MODEL_CALLS_SQL = """
CREATE TABLE IF NOT EXISTS model_calls (
    model_call_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    operation_id TEXT,
    step_id TEXT,
    step_sequence INTEGER CHECK (step_sequence IS NULL OR step_sequence >= 1),
    request_attempt INTEGER NOT NULL CHECK (request_attempt >= 1),
    model_role TEXT NOT NULL
        CHECK (model_role IN ('primary', 'worker', 'utility')),
    purpose TEXT NOT NULL
        CHECK (purpose IN ('agent_step', 'title', 'history_compaction')),
    provider TEXT NOT NULL,
    api_kind TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    requested_model TEXT NOT NULL,
    returned_model TEXT,
    status TEXT NOT NULL
        CHECK (status IN (
            'prepared', 'in_flight', 'completed',
            'failed', 'cancelled', 'incomplete'
        )),
    request_content_ref TEXT NOT NULL,
    response_content_ref TEXT,
    context_fingerprint TEXT,
    provider_request_id TEXT,
    http_status INTEGER
        CHECK (http_status IS NULL OR (http_status BETWEEN 100 AND 599)),
    error_json TEXT
        CHECK (
            error_json IS NULL
            OR (json_valid(error_json) AND json_type(error_json) = 'object')
        ),
    created_at TEXT NOT NULL,
    started_at TEXT,
    first_chunk_at TEXT,
    finished_at TEXT,
    FOREIGN KEY (session_id) REFERENCES conversation_sessions(session_id)
        ON DELETE CASCADE,
    FOREIGN KEY (session_id, operation_id)
        REFERENCES session_operations(session_id, operation_id)
        ON DELETE CASCADE
        DEFERRABLE INITIALLY DEFERRED,
    CHECK (
        (
            purpose = 'agent_step'
            AND operation_id IS NOT NULL
            AND step_id IS NOT NULL
            AND step_sequence IS NOT NULL
            AND context_fingerprint IS NOT NULL
            AND model_role = 'primary'
        )
        OR (
            purpose = 'title'
            AND operation_id IS NULL
            AND step_id IS NULL
            AND step_sequence IS NULL
            AND context_fingerprint IS NULL
            AND model_role = 'utility'
        )
        OR (
            purpose = 'history_compaction'
            AND operation_id IS NULL
            AND step_id IS NULL
            AND step_sequence IS NULL
            AND context_fingerprint IS NULL
            AND model_role = 'worker'
        )
    ),
    CHECK (first_chunk_at IS NULL OR started_at IS NOT NULL),
    CHECK (
        (
            status = 'prepared'
            AND started_at IS NULL
            AND first_chunk_at IS NULL
            AND finished_at IS NULL
            AND response_content_ref IS NULL
            AND error_json IS NULL
        )
        OR (
            status = 'in_flight'
            AND started_at IS NOT NULL
            AND finished_at IS NULL
            AND response_content_ref IS NULL
            AND error_json IS NULL
        )
        OR (
            status = 'completed'
            AND started_at IS NOT NULL
            AND finished_at IS NOT NULL
            AND response_content_ref IS NOT NULL
            AND error_json IS NULL
        )
        OR (
            status = 'failed'
            AND started_at IS NOT NULL
            AND finished_at IS NOT NULL
            AND error_json IS NOT NULL
        )
        OR (
            status IN ('cancelled', 'incomplete')
            AND finished_at IS NOT NULL
        )
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_model_calls_operation_step_attempt
ON model_calls(operation_id, step_id, request_attempt)
WHERE operation_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_model_calls_session_created
ON model_calls(session_id, created_at, model_call_id);
"""


def create_schema(connection: sqlite3.Connection) -> None:
    """在连接上创建 SQLite v12 schema，只接受空库或 v12。"""

    connection.execute("PRAGMA foreign_keys = ON")
    version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if version not in (0, SCHEMA_VERSION):
        raise UnsupportedSchemaVersionError(
            f"不支持直接创建 SQLite schema version {version}；"
            f"需要 0 或 {SCHEMA_VERSION}"
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
    """在调用方事务内创建完整 v12 领域对象。"""

    create_v11_schema_objects(connection)
    create_model_call_schema_objects(connection)


def create_model_call_schema_objects(connection: sqlite3.Connection) -> None:
    """只创建 v12 新增的 ModelCall 表与索引。"""

    statement_lines: list[str] = []
    for line in MODEL_CALLS_SQL.splitlines(keepends=True):
        statement_lines.append(line)
        statement = "".join(statement_lines)
        if not sqlite3.complete_statement(statement):
            continue
        statement_lines.clear()
        stripped = statement.strip()
        if stripped:
            connection.execute(stripped)
    if any(line.strip() for line in statement_lines):
        raise ValueError("SQLite v12 ModelCall schema SQL 包含未结束的语句")


def initialize_schema(connection: sqlite3.Connection) -> None:
    create_schema(connection)


SCHEMA_V12_SQL = MODEL_CALLS_SQL

__all__ = [
    "MODEL_CALLS_SQL",
    "SCHEMA_V12_SQL",
    "SCHEMA_VERSION",
    "UnsupportedSchemaVersionError",
    "create_model_call_schema_objects",
    "create_schema",
    "create_schema_objects",
    "initialize_schema",
]
