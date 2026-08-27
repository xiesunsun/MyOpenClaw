"""SQLite v11 领域 schema。

v11 是 Delegation 跨 Package 精确绑定的当前 Runtime schema。该模块保留完整
的当前领域 DDL，不通过重命名 v10 模块提供运行时兼容分支。
"""

from __future__ import annotations

import sqlite3

SCHEMA_VERSION = 11


class UnsupportedSchemaVersionError(RuntimeError):
    """数据库不是 v11，不能直接套用目标 schema。"""


SCHEMA_SQL = """
PRAGMA user_version = 11;

CREATE TABLE IF NOT EXISTS workspaces (
    workspace_id TEXT PRIMARY KEY,
    root_path TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_package_versions (
    package_version_id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    format_version INTEGER NOT NULL CHECK (format_version >= 1),
    content_json TEXT NOT NULL
        CHECK (json_valid(content_json) AND json_type(content_json) = 'object'),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS conversation_sessions (
    session_id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    cwd TEXT NOT NULL,
    active_node_id TEXT,
    active_operation_id TEXT,
    title TEXT,
    title_source TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    archived_at TEXT,
    CHECK (
        (title IS NULL AND title_source IS NULL)
        OR (title IS NOT NULL AND title_source IN ('generated', 'user'))
    ),
    CHECK (archived_at IS NULL OR active_operation_id IS NULL),
    FOREIGN KEY (workspace_id) REFERENCES workspaces(workspace_id)
        ON DELETE RESTRICT,
    FOREIGN KEY (session_id, active_node_id)
        REFERENCES conversation_nodes(session_id, node_id)
        DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY (session_id, active_operation_id)
        REFERENCES session_operations(session_id, operation_id)
        DEFERRABLE INITIALLY DEFERRED
);

CREATE TABLE IF NOT EXISTS conversation_nodes (
    node_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    parent_node_id TEXT,
    content_type TEXT NOT NULL
        CHECK (content_type IN ('agent_message', 'history_compaction')),
    content_json TEXT NOT NULL
        CHECK (json_valid(content_json) AND json_type(content_json) = 'object'),
    created_at TEXT NOT NULL,
    UNIQUE (session_id, node_id),
    FOREIGN KEY (session_id) REFERENCES conversation_sessions(session_id)
        ON DELETE CASCADE,
    FOREIGN KEY (session_id, parent_node_id)
        REFERENCES conversation_nodes(session_id, node_id)
        ON DELETE CASCADE
        DEFERRABLE INITIALLY DEFERRED
);

CREATE TABLE IF NOT EXISTS agent_inbox_messages (
    message_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    sequence INTEGER NOT NULL CHECK (sequence >= 1),
    delivery TEXT NOT NULL CHECK (delivery IN ('followup', 'steer', 'inject')),
    message_json TEXT NOT NULL
        CHECK (json_valid(message_json) AND json_type(message_json) = 'object'),
    status TEXT NOT NULL CHECK (status IN ('pending', 'claimed', 'discarded')),
    claimed_operation_id TEXT,
    claimed_step_id TEXT,
    outcome_reason TEXT,
    created_at TEXT NOT NULL,
    handled_at TEXT,
    UNIQUE (session_id, sequence),
    UNIQUE (session_id, message_id),
    FOREIGN KEY (session_id) REFERENCES conversation_sessions(session_id)
        ON DELETE CASCADE,
    FOREIGN KEY (session_id, claimed_operation_id)
        REFERENCES session_operations(session_id, operation_id)
        DEFERRABLE INITIALLY DEFERRED,
    CHECK (
        (status = 'pending'
            AND claimed_operation_id IS NULL
            AND claimed_step_id IS NULL
            AND outcome_reason IS NULL
            AND handled_at IS NULL)
        OR (status = 'claimed'
            AND claimed_operation_id IS NOT NULL
            AND outcome_reason IS NULL
            AND handled_at IS NOT NULL)
        OR (status = 'discarded'
            AND claimed_operation_id IS NULL
            AND claimed_step_id IS NULL
            AND outcome_reason IS NOT NULL
            AND handled_at IS NOT NULL)
    )
);

CREATE TABLE IF NOT EXISTS session_operations (
    operation_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    agent_package_version_id TEXT NOT NULL,
    workspace_binding_json TEXT NOT NULL
        CHECK (
            json_valid(workspace_binding_json)
            AND json_type(workspace_binding_json) = 'object'
        ),
    input_node_id TEXT NOT NULL,
    accepted_at TEXT NOT NULL,
    UNIQUE (session_id, operation_id),
    FOREIGN KEY (session_id) REFERENCES conversation_sessions(session_id)
        ON DELETE CASCADE,
    FOREIGN KEY (agent_package_version_id)
        REFERENCES agent_package_versions(package_version_id)
        ON DELETE RESTRICT,
    FOREIGN KEY (session_id, input_node_id)
        REFERENCES conversation_nodes(session_id, node_id)
        ON DELETE RESTRICT
        DEFERRABLE INITIALLY DEFERRED
);

CREATE TABLE IF NOT EXISTS agent_run_states (
    operation_id TEXT PRIMARY KEY,
    revision INTEGER NOT NULL CHECK (revision >= 1),
    status TEXT NOT NULL
        CHECK (status IN ('queued', 'running', 'waiting', 'cancelling',
                          'succeeded', 'failed', 'cancelled')),
    waiting_reason TEXT
        CHECK (waiting_reason IS NULL
               OR waiting_reason IN ('tool_approval', 'tool_reconciliation')),
    completed_step_count INTEGER NOT NULL DEFAULT 0
        CHECK (completed_step_count >= 0),
    current_step_json TEXT
        CHECK (current_step_json IS NULL
               OR (json_valid(current_step_json)
                   AND json_type(current_step_json) = 'object')),
    final_assistant_node_id TEXT,
    error_json TEXT
        CHECK (error_json IS NULL
               OR (json_valid(error_json) AND json_type(error_json) = 'object')),
    cancellation_json TEXT
        CHECK (
            cancellation_json IS NULL
            OR (json_valid(cancellation_json)
                AND json_type(cancellation_json) = 'object')
        ),
    updated_at TEXT NOT NULL,
    FOREIGN KEY (operation_id) REFERENCES session_operations(operation_id)
        ON DELETE CASCADE,
    FOREIGN KEY (final_assistant_node_id)
        REFERENCES conversation_nodes(node_id)
        ON DELETE RESTRICT,
    CHECK (
        (status = 'waiting' AND waiting_reason IS NOT NULL)
        OR (status <> 'waiting' AND waiting_reason IS NULL)
    ),
    CHECK (
        (status IN ('cancelling', 'cancelled') AND cancellation_json IS NOT NULL)
        OR (status NOT IN ('cancelling', 'cancelled') AND cancellation_json IS NULL)
    ),
    CHECK (
        (status = 'failed' AND error_json IS NOT NULL)
        OR (status <> 'failed' AND error_json IS NULL)
    ),
    CHECK (
        (status = 'succeeded' AND final_assistant_node_id IS NOT NULL)
        OR (status <> 'succeeded' AND final_assistant_node_id IS NULL)
    )
);

CREATE TABLE IF NOT EXISTS artifacts (
    artifact_id TEXT PRIMARY KEY,
    size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_delegations (
    child_session_id TEXT PRIMARY KEY,
    child_package_version_id TEXT NOT NULL,
    parent_operation_id TEXT NOT NULL,
    parent_step_id TEXT NOT NULL,
    parent_tool_call_id TEXT NOT NULL UNIQUE,
    initial_message_id TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    FOREIGN KEY (child_session_id) REFERENCES conversation_sessions(session_id)
        ON DELETE CASCADE,
    FOREIGN KEY (child_package_version_id)
        REFERENCES agent_package_versions(package_version_id)
        ON DELETE RESTRICT,
    FOREIGN KEY (parent_operation_id) REFERENCES session_operations(operation_id)
        ON DELETE RESTRICT,
    FOREIGN KEY (child_session_id, initial_message_id)
        REFERENCES agent_inbox_messages(session_id, message_id)
        ON DELETE CASCADE
        DEFERRABLE INITIALLY DEFERRED
);

CREATE INDEX IF NOT EXISTS idx_conversation_sessions_cwd_updated
ON conversation_sessions(cwd, updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_conversation_nodes_session_parent
ON conversation_nodes(session_id, parent_node_id);

CREATE INDEX IF NOT EXISTS idx_agent_inbox_messages_pending
ON agent_inbox_messages(session_id, status, sequence);

CREATE INDEX IF NOT EXISTS idx_agent_package_versions_agent_created
ON agent_package_versions(agent_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_session_operations_session_accepted
ON session_operations(session_id, accepted_at, operation_id);

CREATE INDEX IF NOT EXISTS idx_agent_run_states_status
ON agent_run_states(status, updated_at);

CREATE INDEX IF NOT EXISTS idx_agent_delegations_parent
ON agent_delegations(parent_operation_id, created_at);

CREATE TRIGGER IF NOT EXISTS trg_agent_run_state_final_node_same_session_insert
BEFORE INSERT ON agent_run_states
WHEN NEW.final_assistant_node_id IS NOT NULL
 AND NOT EXISTS (
     SELECT 1
     FROM session_operations AS operation
     JOIN conversation_nodes AS node ON node.node_id = NEW.final_assistant_node_id
     WHERE operation.operation_id = NEW.operation_id
       AND node.session_id = operation.session_id
 )
BEGIN
    SELECT RAISE(ABORT, 'final_assistant_node_id must belong to operation session');
END;

CREATE TRIGGER IF NOT EXISTS trg_agent_run_state_final_node_same_session_update
BEFORE UPDATE OF operation_id, final_assistant_node_id ON agent_run_states
WHEN NEW.final_assistant_node_id IS NOT NULL
 AND NOT EXISTS (
     SELECT 1
     FROM session_operations AS operation
     JOIN conversation_nodes AS node ON node.node_id = NEW.final_assistant_node_id
     WHERE operation.operation_id = NEW.operation_id
       AND node.session_id = operation.session_id
 )
BEGIN
    SELECT RAISE(ABORT, 'final_assistant_node_id must belong to operation session');
END;

"""


def create_schema(connection: sqlite3.Connection) -> None:
    """在连接上创建 SQLite v11 schema，只接受空库或 v11。"""

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
    """在调用方事务内逐条创建 v11 表、索引和触发器。"""

    statement_lines: list[str] = []
    for line in SCHEMA_SQL.splitlines(keepends=True):
        statement_lines.append(line)
        statement = "".join(statement_lines)
        if not sqlite3.complete_statement(statement):
            continue
        statement_lines.clear()
        stripped = statement.strip()
        if stripped and not stripped.upper().startswith("PRAGMA USER_VERSION"):
            connection.execute(stripped)
    if any(line.strip() for line in statement_lines):
        raise ValueError("SQLite v11 schema SQL 包含未结束的语句")


def initialize_schema(connection: sqlite3.Connection) -> None:
    """``create_schema`` 的语义别名，供启动代码使用。"""

    create_schema(connection)


SCHEMA_V11_SQL = SCHEMA_SQL

__all__ = [
    "SCHEMA_SQL",
    "SCHEMA_V11_SQL",
    "SCHEMA_VERSION",
    "UnsupportedSchemaVersionError",
    "create_schema",
    "create_schema_objects",
    "initialize_schema",
]
