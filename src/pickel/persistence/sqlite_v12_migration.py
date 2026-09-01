"""一次性将 Runtime SQLite v12 库转换为 v13。"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pickel.conversations.agent_message import agent_message_from_dict
from pickel.conversations.conversation_node import ConversationNode

V12 = 12
V13 = 13


class SQLiteV12MigrationError(RuntimeError):
    """v12 → v13 迁移前置条件、内容或引用校验失败。"""


@dataclass(frozen=True)
class SQLiteV12MigrationResult:
    backup_path: Path
    checkpoint_count: int


def migrate_v12_to_v13(
    db_path: Path,
    *,
    backup_path: Path | None = None,
) -> SQLiteV12MigrationResult:
    """原地迁移 v12 数据库，并保留一致的 v12 备份。"""

    db_path = Path(db_path)
    if not db_path.is_file():
        raise SQLiteV12MigrationError(f"v12 数据库不存在: {db_path}")
    backup = (
        Path(backup_path) if backup_path is not None else Path(f"{db_path}.v12.bak")
    )
    if backup == db_path:
        raise SQLiteV12MigrationError("v12 备份路径不能与数据库相同")
    if backup.exists():
        raise SQLiteV12MigrationError(f"v12 备份已存在，不覆盖已有备份: {backup}")
    backup.parent.mkdir(parents=True, exist_ok=True)

    connection = sqlite3.connect(db_path)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version != V12:
            raise SQLiteV12MigrationError(
                f"只支持从 SQLite schema v12 迁移，实际版本为 {version}"
            )
        _create_online_backup(connection, backup)
    finally:
        connection.close()

    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        connection.execute("BEGIN IMMEDIATE")
        checkpoint_count = _migrate_checkpoints(connection)
        _validate_database(connection)
        connection.execute(f"PRAGMA user_version = {V13}")
        connection.commit()
    except Exception as exc:
        connection.rollback()
        if isinstance(exc, SQLiteV12MigrationError):
            raise
        raise SQLiteV12MigrationError(f"SQLite v12 → v13 迁移失败: {exc}") from exc
    finally:
        connection.close()
    return SQLiteV12MigrationResult(
        backup_path=backup, checkpoint_count=checkpoint_count
    )


def _create_online_backup(source: sqlite3.Connection, backup_path: Path) -> None:
    destination = sqlite3.connect(backup_path)
    try:
        source.backup(destination)
        destination.commit()
    except Exception as exc:
        destination.rollback()
        destination.close()
        backup_path.unlink(missing_ok=True)
        raise SQLiteV12MigrationError(f"创建 v12 SQLite 一致备份失败: {exc}") from exc
    else:
        destination.close()


def _migrate_checkpoints(connection: sqlite3.Connection) -> int:
    rows = connection.execute(
        "SELECT node_id, session_id, parent_node_id, content_type, content_json, created_at "
        "FROM conversation_nodes ORDER BY node_id"
    ).fetchall()
    by_id = {str(row["node_id"]): row for row in rows}
    checkpoints = [row for row in rows if row["content_type"] == "history_compaction"]
    for row in checkpoints:
        old = _old_checkpoint(str(row["content_json"]), str(row["node_id"]))
        retained = _retained_messages(
            row=row, first_kept_node_id=old["first_kept_node_id"], by_id=by_id
        )
        payload: dict[str, Any] = {
            "summary": old["summary"],
            "retained_messages": retained,
        }
        for key in ("read_files", "modified_files"):
            if key in old:
                payload[key] = old[key]
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        # 先以目标 codec 解码，再写入，确保迁移不会生成不可读内容。
        restored = ConversationNode.from_content_json(
            node_id=str(row["node_id"]),
            session_id=str(row["session_id"]),
            parent_node_id=row["parent_node_id"],
            content_type="history_compaction",
            content_json=encoded,
            created_at=_time(str(row["created_at"])),
        )
        if restored.content_json() != encoded:
            raise SQLiteV12MigrationError(
                f"HistoryCompaction round-trip 失败: {row['node_id']}"
            )
        connection.execute(
            "UPDATE conversation_nodes SET content_json = ? WHERE node_id = ?",
            (encoded, row["node_id"]),
        )
    return len(checkpoints)


def _old_checkpoint(content_json: str, node_id: str) -> dict[str, Any]:
    try:
        value = json.loads(content_json)
    except (TypeError, json.JSONDecodeError) as exc:
        raise SQLiteV12MigrationError(f"旧 checkpoint 内容不可解码: {node_id}") from exc
    if not isinstance(value, dict):
        raise SQLiteV12MigrationError(f"旧 checkpoint 内容不是 object: {node_id}")
    allowed = {"summary", "first_kept_node_id", "read_files", "modified_files"}
    if (
        set(value) - allowed
        or "summary" not in value
        or "first_kept_node_id" not in value
    ):
        raise SQLiteV12MigrationError(f"旧 checkpoint 字段无效: {node_id}")
    if not isinstance(value["summary"], str) or not value["summary"]:
        raise SQLiteV12MigrationError(f"旧 checkpoint summary 无效: {node_id}")
    first = value["first_kept_node_id"]
    if not isinstance(first, str) or not first:
        raise SQLiteV12MigrationError(
            f"旧 checkpoint first_kept_node_id 无效: {node_id}"
        )
    for key in ("read_files", "modified_files"):
        if key in value and (
            not isinstance(value[key], list)
            or not all(isinstance(item, str) for item in value[key])
        ):
            raise SQLiteV12MigrationError(f"旧 checkpoint {key} 无效: {node_id}")
    return value


def _retained_messages(
    *, row: sqlite3.Row, first_kept_node_id: str, by_id: dict[str, sqlite3.Row]
) -> list[dict[str, Any]]:
    session_id = str(row["session_id"])
    current_id = row["parent_node_id"]
    path: list[sqlite3.Row] = []
    visited: set[str] = set()
    while current_id is not None:
        current_id = str(current_id)
        if current_id in visited:
            raise SQLiteV12MigrationError(
                f"旧 checkpoint parent 链循环: {row['node_id']}"
            )
        visited.add(current_id)
        current = by_id.get(current_id)
        if current is None or str(current["session_id"]) != session_id:
            raise SQLiteV12MigrationError(
                f"旧 checkpoint 引用跨 Session 或不存在: {row['node_id']}"
            )
        path.append(current)
        if current_id == first_kept_node_id:
            break
        current_id = current["parent_node_id"]
    if not path or str(path[-1]["node_id"]) != first_kept_node_id:
        raise SQLiteV12MigrationError(
            f"旧 checkpoint first_kept_node_id 不可达: {row['node_id']}"
        )
    retained: list[dict[str, Any]] = []
    for message_row in reversed(path):
        content_type = str(message_row["content_type"])
        if content_type != "agent_message":
            continue
        try:
            value = json.loads(str(message_row["content_json"]))
            if not isinstance(value, dict):
                raise TypeError("AgentMessage 内容不是 object")
            message = agent_message_from_dict(value)
        except (TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
            raise SQLiteV12MigrationError(
                f"旧 checkpoint retained AgentMessage 不可解码: {row['node_id']}"
            ) from exc
        retained.append(_message_dict(message))
    return retained


def _message_dict(message: Any) -> dict[str, Any]:
    from pickel.conversations.agent_message import agent_message_to_dict

    return agent_message_to_dict(message)


def _validate_database(connection: sqlite3.Connection) -> None:
    violations = connection.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        raise SQLiteV12MigrationError(f"迁移后外键完整性检查失败: {violations[:3]}")
    rows = connection.execute(
        "SELECT node_id, session_id, parent_node_id, content_type, content_json, created_at "
        "FROM conversation_nodes WHERE content_type = 'history_compaction'"
    ).fetchall()
    for row in rows:
        try:
            node = ConversationNode.from_content_json(
                node_id=str(row["node_id"]),
                session_id=str(row["session_id"]),
                parent_node_id=row["parent_node_id"],
                content_type="history_compaction",
                content_json=str(row["content_json"]),
                created_at=_time(str(row["created_at"])),
            )
        except (TypeError, ValueError, KeyError) as exc:
            raise SQLiteV12MigrationError(
                f"v13 checkpoint 校验失败: {row['node_id']}"
            ) from exc
        if node.content_json() != str(row["content_json"]):
            raise SQLiteV12MigrationError(
                f"v13 checkpoint 非规范 JSON: {row['node_id']}"
            )


def _time(value: str):
    from datetime import datetime

    return datetime.fromisoformat(value)


migrate = migrate_v12_to_v13

__all__ = [
    "SQLiteV12MigrationError",
    "SQLiteV12MigrationResult",
    "V12",
    "V13",
    "migrate",
    "migrate_v12_to_v13",
]
