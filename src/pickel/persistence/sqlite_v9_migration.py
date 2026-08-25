"""一次性将 Runtime SQLite v9 库转换为 v10。

迁移器只认识旧库的通用持久化表；v10 的 DDL 由 ``sqlite_schema_v10`` 提供。
旧表会在同一个 SQLite 事务中暂时改名，因此任何校验失败都会把数据库恢复为
原来的 v9 形态。迁移成功后不会留下 v9 生产表。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pickel.agents.agent_package import decode_legacy_agent_package
from pickel.conversations.agent_message import (
    UserMessage,
    agent_message_from_dict,
    agent_message_to_dict,
)
from pickel.conversations.content_blocks import TextBlock

V9 = 9
V10 = 10
_V9_TABLES = (
    "sessions",
    "agent_package_versions",
    "artifacts",
    "session_operations",
    "agent_delegations",
    "storage_commits",
    "immutable_objects",
    "conversation_nodes",
    "named_references",
)


class SQLiteV9MigrationError(RuntimeError):
    """迁移前置条件、引用或恢复语义不满足时抛出的稳定错误。"""


@dataclass(frozen=True)
class SQLiteV9MigrationResult:
    backup_path: Path
    session_count: int
    node_count: int
    operation_count: int
    warning_messages: tuple[str, ...] = ()

    @property
    def warnings(self) -> tuple[str, ...]:
        return self.warning_messages


def migrate_v9_to_v10(
    db_path: Path,
    *,
    backup_path: Path | None = None,
) -> SQLiteV9MigrationResult:
    """在原地执行一次 v9 → v10 迁移，并返回显式备份路径。

    ``db_path`` 必须是现有、``PRAGMA user_version = 9`` 的数据库。迁移器不会
    创建缺失的 v9 数据库，也不会覆盖已经存在的备份；重复运行 v10 库会明确失败。
    """

    db_path = Path(db_path)
    if not db_path.is_file():
        raise SQLiteV9MigrationError(f"v9 数据库不存在: {db_path}")
    backup = Path(backup_path) if backup_path is not None else Path(f"{db_path}.v9.bak")
    if backup == db_path:
        raise SQLiteV9MigrationError("v9 备份路径不能与数据库相同")
    if backup.exists():
        raise SQLiteV9MigrationError(f"v9 备份已存在，不覆盖已有备份: {backup}")
    backup.parent.mkdir(parents=True, exist_ok=True)

    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version != V9:
            raise SQLiteV9MigrationError(
                f"只支持从 SQLite schema v9 迁移，实际版本为 {version}"
            )
        # 主文件可能处于 WAL 模式；Online Backup API 会同时读取 WAL 中尚未
        # checkpoint 的页，不能只复制 SQLite 主文件。
        _create_online_backup(connection, backup)
    finally:
        connection.close()

    warnings: list[str] = []
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        connection.execute("BEGIN IMMEDIATE")
        _require_v9_tables(connection)
        _rename_v9_tables(connection)
        _create_v10_schema(connection)
        counts = _copy_v9_facts(connection, warnings)
        _drop_v9_tables(connection)
        connection.execute("PRAGMA user_version = 10")
        connection.commit()
    except Exception as exc:
        connection.rollback()
        if isinstance(exc, SQLiteV9MigrationError):
            raise
        raise SQLiteV9MigrationError(f"SQLite v9 → v10 迁移失败: {exc}") from exc
    finally:
        connection.close()
    return SQLiteV9MigrationResult(backup, *counts, tuple(warnings))


# 简短别名给命令行/测试调用方；主名保留版本边界，避免长期双轨读写。
migrate = migrate_v9_to_v10


def _create_online_backup(
    source: sqlite3.Connection,
    backup_path: Path,
) -> None:
    destination = sqlite3.connect(backup_path)
    try:
        source.backup(destination)
        destination.commit()
    except Exception as exc:
        destination.rollback()
        destination.close()
        backup_path.unlink(missing_ok=True)
        raise SQLiteV9MigrationError(f"创建 v9 SQLite 一致备份失败: {exc}") from exc
    else:
        destination.close()


def _require_v9_tables(connection: sqlite3.Connection) -> None:
    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    ).fetchall()
    names = {str(row["name"]) for row in rows}
    missing = [name for name in _V9_TABLES if name not in names]
    if missing:
        raise SQLiteV9MigrationError(f"v9 数据库缺少旧表: {', '.join(missing)}")


def _rename_v9_tables(connection: sqlite3.Connection) -> None:
    for name in _V9_TABLES:
        connection.execute(f'ALTER TABLE "{name}" RENAME TO "{name}_v9"')


def _create_v10_schema(connection: sqlite3.Connection) -> None:
    try:
        from pickel.persistence import sqlite_schema_v10
    except ImportError as exc:  # pragma: no cover - 由并行 schema 任务提供
        raise SQLiteV9MigrationError(
            "缺少 v10 schema 提供模块 pickel.persistence.sqlite_schema_v10"
        ) from exc

    function = getattr(sqlite_schema_v10, "create_schema_objects", None)
    if not callable(function):
        raise SQLiteV9MigrationError(
            "sqlite_schema_v10 未提供 create_schema_objects(connection)"
        )
    function(connection)


def _copy_v9_facts(
    connection: sqlite3.Connection,
    warnings: list[str],
) -> tuple[int, int, int]:
    objects = _load_objects(connection)
    sessions = _load_sessions(connection)
    packages = _load_packages(connection)
    operations = _load_operations(connection)
    states = _load_latest_states(connection)
    nodes = _load_nodes(connection)
    references = _load_active_references(connection)

    workspace_by_root: dict[str, str] = {}
    session_workspace: dict[str, tuple[str, str]] = {}
    for row in sessions:
        cwd = _normalise_cwd(row["cwd"])
        workspace_id = workspace_by_root.setdefault(cwd, _workspace_id(cwd))
        session_workspace[str(row["session_id"])] = (workspace_id, cwd)
    now = _utc_now()
    for root, workspace_id in workspace_by_root.items():
        _insert(
            connection,
            "workspaces",
            {"workspace_id": workspace_id, "root_path": root, "created_at": now},
        )

    package_id_map: dict[str, str] = {}
    inserted_package_ids: set[str] = set()
    for row in packages:
        content = _json_object(row["content_json"], "AgentPackageVersion.content_json")
        legacy_digest = hashlib.sha256(
            _canonical_json(content).encode("utf-8")
        ).hexdigest()
        legacy_package_id = f"agentpkg_{legacy_digest}"
        old_package_id = str(row["package_version_id"])
        if (
            old_package_id.startswith("agentpkg_")
            and old_package_id != legacy_package_id
        ):
            raise SQLiteV9MigrationError(
                f"v9 AgentPackageVersion ID 与 canonical content 不一致: {old_package_id}"
            )
        old_digest = str(row["digest"]) if row["digest"] is not None else ""
        if len(old_digest) == 64 and old_digest != legacy_digest:
            raise SQLiteV9MigrationError(
                f"v9 AgentPackageVersion digest 校验失败: {old_package_id}"
            )
        try:
            package = decode_legacy_agent_package(
                content=content,
                created_at=datetime.fromisoformat(str(row["created_at"])),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise SQLiteV9MigrationError(
                f"v9 AgentPackageVersion 无法转换: {old_package_id}"
            ) from exc
        package_id = package.package_version_id
        package_id_map[old_package_id] = package_id
        if package_id in inserted_package_ids:
            continue
        _insert(
            connection,
            "agent_package_versions",
            {
                "package_version_id": package_id,
                "agent_id": package.agent_id,
                "format_version": package.format_version,
                "content_json": _canonical_json(package.content_dict()),
                "created_at": str(row["created_at"]),
            },
        )
        inserted_package_ids.add(package_id)

    migrated_artifact_ids: set[str] = set()
    for row in connection.execute("SELECT * FROM artifacts_v9").fetchall():
        artifact_id = str(row["artifact_id"])
        digest = str(row["digest"])
        if artifact_id != f"artifact_{digest}":
            raise SQLiteV9MigrationError(f"Artifact ID 与 digest 不一致: {artifact_id}")
        _insert(
            connection,
            "artifacts",
            {
                "artifact_id": artifact_id,
                "size_bytes": int(row["size_bytes"]),
                "created_at": str(row["created_at"]),
            },
        )
        migrated_artifact_ids.add(artifact_id)

    for row in sessions:
        session_id = str(row["session_id"])
        workspace_id, cwd = session_workspace[session_id]
        archived_at = (
            str(row["updated_at"]) if str(row["status"]) == "archived" else None
        )
        if archived_at is not None:
            warnings.append(f"Session {session_id} 使用 v9 updated_at 作为 archived_at")
        title = row["title"]
        _insert(
            connection,
            "conversation_sessions",
            {
                "session_id": session_id,
                "agent_id": str(row["agent_id"]),
                "workspace_id": workspace_id,
                "cwd": cwd,
                # Node 尚未写入；迁移完 Node 后再设置活动位置，兼容即时 FK。
                "active_node_id": None,
                "active_operation_id": None,
                "title": str(title) if title is not None else None,
                "title_source": "user" if title is not None else None,
                "created_at": str(row["created_at"]),
                "updated_at": str(row["updated_at"]),
                "archived_at": archived_at,
            },
        )

    node_ids: set[str] = set()
    node_session: dict[str, str] = {}
    node_content: dict[str, dict[str, Any]] = {}
    for row in nodes:
        node_id = str(row["node_id"])
        session_id = str(row["session_id"])
        if session_id not in session_workspace:
            raise SQLiteV9MigrationError(
                f"ConversationNode 属于未知 Session: {node_id}"
            )
        object_id = str(row["object_id"])
        if object_id not in objects:
            raise SQLiteV9MigrationError(
                f"ConversationNode 指向未知 ImmutableObject: {node_id}"
            )
        object_type, content = objects[object_id]
        content_type = _node_content_type(object_type)
        normalized_content = _normalize_node_content(
            content_type, content, migrated_artifact_ids
        )
        node_ids.add(node_id)
        node_session[node_id] = session_id
        node_content[node_id] = normalized_content
        _insert(
            connection,
            "conversation_nodes",
            {
                "node_id": node_id,
                "session_id": session_id,
                "parent_node_id": row["parent_node_id"],
                "content_type": content_type,
                "content_json": _canonical_json(normalized_content),
                "created_at": str(row["created_at"]),
            },
        )
    for session_id, active_node_id in references.items():
        if active_node_id not in node_ids:
            raise SQLiteV9MigrationError(
                f"active NamedReference 指向不存在的 Node: {session_id}/{active_node_id}"
            )
        connection.execute(
            "UPDATE conversation_sessions SET active_node_id = ? WHERE session_id = ?",
            (active_node_id, session_id),
        )

    operation_input: dict[str, str] = {}
    for row in operations:
        operation_id = str(row["operation_id"])
        session_id = str(row["session_id"])
        old_package_id = str(row["agent_package_version_id"])
        package_id = package_id_map.get(old_package_id)
        if package_id is None:
            raise SQLiteV9MigrationError(
                f"Operation 引用了不存在的 AgentPackageVersion: {old_package_id}"
            )
        if session_id not in session_workspace:
            raise SQLiteV9MigrationError(f"Operation 属于未知 Session: {operation_id}")
        input_node_id = _operation_input_node(
            states.get(operation_id), nodes, session_id
        )
        if (
            input_node_id is None
            or input_node_id not in node_ids
            or node_session[input_node_id] != session_id
        ):
            raise SQLiteV9MigrationError(
                f"Operation 无法唯一推导 input_node_id: {operation_id}"
            )
        operation_input[operation_id] = input_node_id
        workspace_id, cwd = session_workspace[session_id]
        _insert(
            connection,
            "session_operations",
            {
                "operation_id": operation_id,
                "session_id": session_id,
                "agent_package_version_id": package_id,
                "workspace_binding_json": _canonical_json(
                    {
                        "workspace_id": workspace_id,
                        "working_directory": cwd,
                        "allowed_root": cwd,
                    }
                ),
                "input_node_id": input_node_id,
                "accepted_at": str(row["created_at"]),
            },
        )

    # v9 没有 Inbox。已被旧 Operation 接受的输入以 claimed 消息保留，供
    # Delegation.initial_message_id 建立可校验的因果闭包；普通 Session 不凭空造消息。
    delegated_children = {
        str(row["child_operation_id"]): row
        for row in connection.execute("SELECT * FROM agent_delegations_v9").fetchall()
    }
    operation_session = {
        str(row["operation_id"]): str(row["session_id"]) for row in operations
    }
    for child_operation_id, row in delegated_children.items():
        input_node_id = operation_input.get(child_operation_id)
        child_session_id = str(row["child_session_id"])
        if input_node_id is None or child_session_id not in session_workspace:
            raise SQLiteV9MigrationError(
                f"Delegation 初始消息无法唯一推导: {child_operation_id}"
            )
        message = node_content.get(input_node_id)
        if message is None:
            raise SQLiteV9MigrationError(
                f"Delegation 初始消息 Node 不存在: {input_node_id}"
            )
        parent_operation_id = str(row["parent_operation_id"])
        parent_session_id = operation_session.get(parent_operation_id)
        if parent_session_id is None or not _is_user_message(message):
            raise SQLiteV9MigrationError(
                f"Delegation 初始消息不是可唯一恢复的 UserMessage: {child_operation_id}"
            )
        message_payload = {
            "message": _user_message_payload(message),
            "source": {
                "kind": "agent",
                "sender_session_id": parent_session_id,
                "sender_operation_id": parent_operation_id,
                "form": "followup",
            },
        }
        _insert(
            connection,
            "agent_inbox_messages",
            {
                "message_id": input_node_id,
                "session_id": child_session_id,
                "sequence": 1,
                "delivery": "followup",
                "message_json": _canonical_json(message_payload),
                "status": "claimed",
                "claimed_operation_id": child_operation_id,
                "claimed_step_id": None,
                "outcome_reason": None,
                "created_at": str(row["created_at"]),
                "handled_at": str(row["created_at"]),
            },
        )

    operation_ids = set(operation_input)
    orphan_state_ids = set(states) - operation_ids
    if orphan_state_ids:
        raise SQLiteV9MigrationError(
            "Operation State 引用了不存在的 Operation: "
            + ", ".join(sorted(orphan_state_ids))
        )
    for operation_id in operation_input:
        _insert_state(connection, operation_id, states.get(operation_id), nodes)

    for row in delegated_children.values():
        parent_operation_id = str(row["parent_operation_id"])
        child_operation_id = str(row["child_operation_id"])
        if (
            parent_operation_id not in operation_input
            or child_operation_id not in operation_input
        ):
            raise SQLiteV9MigrationError("Delegation 引用了不存在的 Operation")
        parent_tool_call_id = row["parent_tool_call_id"]
        if parent_tool_call_id is None:
            raise SQLiteV9MigrationError(
                "Delegation 缺少可唯一推导的 parent_tool_call_id"
            )
        _insert(
            connection,
            "agent_delegations",
            {
                "child_session_id": str(row["child_session_id"]),
                "parent_operation_id": parent_operation_id,
                "parent_step_id": str(row["parent_step_id"]),
                "parent_tool_call_id": str(parent_tool_call_id),
                "initial_message_id": operation_input[child_operation_id],
                "created_at": str(row["created_at"]),
            },
        )

    return len(sessions), len(nodes), len(operations)


def _insert_state(
    connection: sqlite3.Connection,
    operation_id: str,
    state: tuple[dict[str, Any], str] | None,
    nodes: list[sqlite3.Row],
) -> None:
    content, updated_at = state if state is not None else ({}, _utc_now())
    status = str(content.get("status", "queued"))
    terminal = status in {"succeeded", "failed", "cancelled"}
    error = content.get("error") if isinstance(content.get("error"), dict) else None
    cancellation = content.get("cancellation")
    if status not in {
        "queued",
        "running",
        "waiting",
        "cancelling",
        "succeeded",
        "failed",
        "cancelled",
    }:
        terminal = False
    if not terminal:
        # v9 没有冻结 ModelContext/Intent；禁止把未知副作用静默重放。
        status = "failed"
        error = {
            "code": "v9_migration_unrecoverable_operation",
            "message": "v9 Operation 缺少可恢复的冻结状态，迁移后不重放",
            "retryable": True,
        }
        cancellation = None
    if status == "failed" and error is None:
        error = {
            "code": "v9_migration_failed_operation",
            "message": "Operation 在 v9 中已失败",
            "retryable": False,
        }
    if status == "failed" and error is not None:
        error = dict(error)
        error.setdefault("retryable", False)
    if status == "cancelled" and not isinstance(cancellation, dict):
        cancellation = {"reason": "migrated_from_v9"}
    final_node = content.get("final_assistant_node_id")
    if status == "succeeded" and (
        not final_node or str(final_node) not in {str(row["node_id"]) for row in nodes}
    ):
        raise SQLiteV9MigrationError(
            f"succeeded Operation 缺少最终 Assistant Node: {operation_id}"
        )
    _insert(
        connection,
        "agent_run_states",
        {
            "operation_id": operation_id,
            "revision": max(1, int(content.get("revision", 1))),
            "status": status,
            "waiting_reason": None,
            "completed_step_count": len(content.get("completed_step_ids") or ()),
            # 终态只保存结果事实；旧 phase 不能污染 v10 当前步骤。
            "current_step_json": None,
            "final_assistant_node_id": (
                str(final_node) if status == "succeeded" else None
            ),
            "error_json": (
                _canonical_json(error)
                if status == "failed" and error is not None
                else None
            ),
            "cancellation_json": (
                _canonical_json(cancellation)
                if isinstance(cancellation, dict)
                else None
            ),
            "updated_at": updated_at,
        },
    )


def _load_objects(
    connection: sqlite3.Connection,
) -> dict[str, tuple[str, dict[str, Any]]]:
    result: dict[str, tuple[str, dict[str, Any]]] = {}
    for row in connection.execute("SELECT * FROM immutable_objects_v9").fetchall():
        result[str(row["object_id"])] = (
            str(row["object_type"]),
            _json_object(row["content_json"], "ImmutableObject.content_json"),
        )
    return result


def _load_sessions(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    return connection.execute(
        "SELECT * FROM sessions_v9 ORDER BY session_id"
    ).fetchall()


def _load_packages(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    return connection.execute("SELECT * FROM agent_package_versions_v9").fetchall()


def _load_operations(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    return connection.execute(
        "SELECT * FROM session_operations_v9 ORDER BY operation_id"
    ).fetchall()


def _load_nodes(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    return connection.execute(
        "SELECT * FROM conversation_nodes_v9 ORDER BY created_commit_sequence, node_id"
    ).fetchall()


def _load_active_references(connection: sqlite3.Connection) -> dict[str, str]:
    rows = connection.execute("""
        SELECT session_id, target_id, target_kind
        FROM named_references_v9
        WHERE reference_name = 'conversation/active'
        ORDER BY session_id, commit_sequence DESC
        """).fetchall()
    result: dict[str, str] = {}
    for row in rows:
        session_id = str(row["session_id"])
        if session_id in result:
            continue
        if str(row["target_kind"]) != "node":
            raise SQLiteV9MigrationError(
                f"active NamedReference 必须指向 Node: {session_id}"
            )
        result[session_id] = str(row["target_id"])
    return result


def _load_latest_states(
    connection: sqlite3.Connection,
) -> dict[str, tuple[dict[str, Any], str]]:
    rows = connection.execute("""
        SELECT reference.target_id, object.content_json, object.created_at
        FROM named_references_v9 AS reference
        JOIN immutable_objects_v9 AS object ON object.object_id = reference.target_id
        WHERE reference.reference_name LIKE 'operation/%/state'
           OR reference.reference_name LIKE 'operation/state/%'
        ORDER BY reference.commit_sequence DESC
        """).fetchall()
    result: dict[str, tuple[dict[str, Any], str]] = {}
    for row in rows:
        content = _json_object(row["content_json"], "Operation State.content_json")
        operation_id = str(content.get("operation_id", ""))
        if operation_id and operation_id not in result:
            result[operation_id] = (content, str(row["created_at"]))
    return result


def _operation_input_node(
    state: tuple[dict[str, Any], str] | None,
    nodes: list[sqlite3.Row],
    session_id: str,
) -> str | None:
    if state is not None:
        value = state[0].get("user_message_node_id")
        if value is not None:
            return str(value)
    candidates: list[str] = []
    for row in nodes:
        if str(row["session_id"]) != session_id:
            continue
        # 没有 State 时只接受唯一的根 Node，避免把活动位置猜成输入。
        if row["parent_node_id"] is None:
            candidates.append(str(row["node_id"]))
    return candidates[0] if len(candidates) == 1 else None


def _node_content_type(object_type: str) -> str:
    if object_type in {"history_compaction", "conversation_history_compaction"}:
        return "history_compaction"
    if object_type in {
        "agent_message",
        "user_message",
        "assistant_message",
        "tool_result",
    }:
        return "agent_message"
    raise SQLiteV9MigrationError(
        f"无法将旧 Object 类型迁移为 ConversationNode: {object_type}"
    )


def _normalize_node_content(
    content_type: str,
    content: dict[str, Any],
    artifact_ids: set[str],
) -> dict[str, Any]:
    if content_type == "history_compaction":
        summary = content.get("summary")
        first_kept_node_id = content.get("first_kept_node_id")
        if not isinstance(summary, str) or not summary:
            raise SQLiteV9MigrationError("HistoryCompaction.summary 无法解析")
        if first_kept_node_id is not None and not isinstance(first_kept_node_id, str):
            raise SQLiteV9MigrationError(
                "HistoryCompaction.first_kept_node_id 无法解析"
            )
        return {
            "summary": summary,
            "first_kept_node_id": first_kept_node_id,
        }
    return _normalize_agent_message(content, artifact_ids)


def _normalize_agent_message(
    content: dict[str, Any],
    artifact_ids: set[str],
) -> dict[str, Any]:
    """解析 v1/v2/v3 AgentMessage，并输出唯一的 v10 payload。"""

    candidate = json.loads(json.dumps(content, ensure_ascii=False))
    if candidate.get("payload_version") is None:
        if isinstance(candidate.get("content"), list):
            candidate = {
                "payload_version": 3,
                "role": candidate.get("role"),
                "content": candidate["content"],
            }
        elif candidate.get("role") == "user" and isinstance(candidate.get("text"), str):
            candidate = UserMessage(content=[TextBlock(text=candidate["text"])])
            return agent_message_to_dict(candidate)
    blocks = candidate.get("content")
    if not isinstance(blocks, list):
        raise SQLiteV9MigrationError("AgentMessage.content 无法解析")
    for block in blocks:
        if not isinstance(block, dict) or block.get("type") != "artifact":
            continue
        reference = block.get("artifact")
        if not isinstance(reference, dict):
            raise SQLiteV9MigrationError("ArtifactReference 无法解析")
        artifact_id = reference.get("artifact_id")
        if not isinstance(artifact_id, str) or artifact_id not in artifact_ids:
            raise SQLiteV9MigrationError(
                f"AgentMessage 引用了未迁移的 Artifact: {artifact_id}"
            )
        # digest/size_bytes/blob_key 是 v9 冗余字段；ArtifactReference 只保留
        # v10 消息所需的身份和展示字段。
        block["artifact"] = {
            "artifact_id": artifact_id,
            "media_type": reference.get("media_type"),
            "display_name": reference.get("display_name"),
        }
    try:
        message = agent_message_from_dict(candidate)
        return agent_message_to_dict(message)
    except (TypeError, ValueError, KeyError) as exc:
        raise SQLiteV9MigrationError("AgentMessage 无法解析为 v10 payload") from exc


def _is_user_message(content: dict[str, Any]) -> bool:
    return content.get("role") == "user"


def _user_message_payload(content: dict[str, Any]) -> dict[str, Any]:
    """将 v9 的用户节点规范化为 v10 UserMessage wire payload。"""

    if not _is_user_message(content):
        raise SQLiteV9MigrationError("ConversationNode 不是 UserMessage")
    try:
        if content.get("payload_version") is not None:
            message = agent_message_from_dict(content)
        elif isinstance(content.get("content"), list):
            message = agent_message_from_dict(
                {"payload_version": 3, "role": "user", "content": content["content"]}
            )
        else:
            text = content.get("text")
            if not isinstance(text, str):
                raise ValueError("旧 UserMessage 缺少 text/content")
            message = UserMessage(content=[TextBlock(text=text)])
        if not isinstance(message, UserMessage):
            raise ValueError("节点不是 UserMessage")
        return agent_message_to_dict(message)
    except (TypeError, ValueError, KeyError) as exc:
        raise SQLiteV9MigrationError(
            "ConversationNode 不是可恢复的 UserMessage"
        ) from exc


def _drop_v9_tables(connection: sqlite3.Connection) -> None:
    for name in reversed(_V9_TABLES):
        connection.execute(f'DROP TABLE "{name}_v9"')


def _insert(connection: sqlite3.Connection, table: str, values: dict[str, Any]) -> None:
    columns = _table_columns(connection, table)
    filtered = {key: value for key, value in values.items() if key in columns}
    missing = [
        key
        for key in columns
        if key not in filtered and key in _required_columns(connection, table)
    ]
    if missing:
        raise SQLiteV9MigrationError(
            f"v10 表 {table} 缺少迁移字段: {', '.join(missing)}"
        )
    names = ", ".join(f'"{key}"' for key in filtered)
    placeholders = ", ".join("?" for _ in filtered)
    try:
        connection.execute(
            f'INSERT INTO "{table}" ({names}) VALUES ({placeholders})',
            tuple(filtered.values()),
        )
    except sqlite3.IntegrityError as exc:
        raise SQLiteV9MigrationError(f"写入 v10.{table} 失败: {exc}") from exc


def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {
        str(row["name"]) for row in connection.execute(f'PRAGMA table_info("{table}")')
    }


def _required_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {
        str(row["name"])
        for row in connection.execute(f'PRAGMA table_info("{table}")')
        if int(row["notnull"]) and row["dflt_value"] is None and int(row["pk"]) == 0
    }


def _json_object(value: Any, label: str) -> dict[str, Any]:
    try:
        decoded = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise SQLiteV9MigrationError(f"{label} 不是合法 JSON object") from exc
    if not isinstance(decoded, dict):
        raise SQLiteV9MigrationError(f"{label} 必须是 JSON object")
    return decoded


def _canonical_json(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _normalise_cwd(value: Any) -> str:
    try:
        return str(Path(str(value)).expanduser().absolute().resolve())
    except OSError as exc:
        raise SQLiteV9MigrationError(f"无法规范化 Session cwd: {value}") from exc


def _workspace_id(root_path: str) -> str:
    return "workspace_" + hashlib.sha256(root_path.encode("utf-8")).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
