"""SQLite v10 Runtime Store。

该适配器直接读写 v10 领域实体。旧库必须先显式执行一次迁移。
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from pickel.agents.agent_package import (
    AgentPackageVersion,
    decode_agent_package_content,
    package_version_id_for_content,
)
from pickel.artifacts.artifact import Artifact
from pickel.conversations.agent_message import UserMessage
from pickel.conversations.content_blocks import ArtifactBlock, TextBlock
from pickel.conversations.conversation_node import ConversationNode
from pickel.conversations.conversation_session import ConversationSession
from pickel.inbox.message import (
    AgentMessageSource,
    InboxMessage,
    MessageDelivery,
    MessageSource,
)
from pickel.operations.agent_delegation import AgentDelegation
from pickel.operations.agent_run_state import (
    AgentRunState,
    DelegateAgentIntent,
    agent_run_state_from_content,
)
from pickel.operations.session_operation import SessionOperation
from pickel.persistence.errors import StorageConflictError, StorageIntegrityError
from pickel.persistence.sqlite_schema_v10 import (
    SCHEMA_VERSION,
    UnsupportedSchemaVersionError,
    create_schema,
)
from pickel.workspaces.workspace import Workspace


class UnsupportedStorageSchemaError(RuntimeError):
    """数据库需要先经过显式 schema 迁移。"""


_PACKAGE_ID = re.compile(r"^agentpkg_[0-9a-f]{64}$")

_LIST_BRANCH_NODES_SQL = """
    WITH RECURSIVE branch(node_id, parent_node_id, depth) AS (
        SELECT node_id, parent_node_id, 0
        FROM conversation_nodes
        WHERE node_id = ? AND session_id = ?
        UNION ALL
        SELECT parent.node_id, parent.parent_node_id, branch.depth + 1
        FROM conversation_nodes AS parent
        JOIN branch ON parent.node_id = branch.parent_node_id
        WHERE parent.session_id = ?
    )
    SELECT n.*
    FROM conversation_nodes AS n
    JOIN branch AS b ON b.node_id = n.node_id
    ORDER BY b.depth DESC
"""


class SQLiteRuntimeStore:
    """Conversation、Inbox、Operation 等 v10 实体的直接 SQLite 适配器。"""

    def __init__(self, db_path: Path) -> None:
        self._db_path = Path(db_path)

    @property
    def db_path(self) -> Path:
        return self._db_path

    # -- Workspace ---------------------------------------------------------

    def create_session(
        self, *, workspace: Workspace, session: ConversationSession
    ) -> None:
        if not workspace.root_path.is_dir():
            raise ValueError(f"Workspace 根目录不存在: {workspace.root_path}")
        self._ensure_schema()
        if session.workspace_id != workspace.workspace_id:
            raise StorageIntegrityError("Session.workspace_id 与 Workspace 不匹配")
        if any(
            value is not None
            for value in (
                session.active_node_id,
                session.active_operation_id,
                session.title,
                session.title_source,
                session.archived_at,
            )
        ):
            raise StorageIntegrityError("create_session 只能创建空 Session")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    "SELECT * FROM workspaces WHERE workspace_id = ?",
                    (workspace.workspace_id,),
                ).fetchone()
                if existing is None:
                    self._insert_workspace(connection, workspace)
                elif str(existing["root_path"]) != str(workspace.root_path):
                    raise StorageIntegrityError("Workspace ID 已存在但内容不同")
                connection.execute(
                    """
                    INSERT INTO conversation_sessions (
                        session_id, agent_id, workspace_id, cwd,
                        active_node_id, active_operation_id, title, title_source,
                        created_at, updated_at, archived_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        session.session_id,
                        session.agent_id,
                        session.workspace_id,
                        str(session.cwd),
                        session.active_node_id,
                        session.active_operation_id,
                        session.title,
                        session.title_source,
                        session.created_at.isoformat(),
                        session.updated_at.isoformat(),
                        _iso(session.archived_at),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise StorageIntegrityError(
                    "Workspace 或 ConversationSession 写入失败"
                ) from exc

    @staticmethod
    def _insert_workspace(connection: sqlite3.Connection, workspace: Workspace) -> None:
        try:
            connection.execute(
                "INSERT INTO workspaces VALUES (?, ?, ?)",
                (
                    workspace.workspace_id,
                    str(workspace.root_path),
                    workspace.created_at.isoformat(),
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise StorageIntegrityError("Workspace 已存在或 root_path 重复") from exc

    def load_workspace(self, workspace_id: str) -> Workspace | None:
        self._ensure_schema()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM workspaces WHERE workspace_id = ?", (workspace_id,)
            ).fetchone()
        if row is None:
            return None
        return Workspace(
            workspace_id=str(row["workspace_id"]),
            root_path=Path(str(row["root_path"])),
            created_at=_time(row["created_at"]),
        )

    def find_workspace_by_root(self, root_path: str | Path) -> Workspace | None:
        normalized = Path(root_path).expanduser().resolve(strict=False)
        self._ensure_schema()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM workspaces WHERE root_path = ?", (str(normalized),)
            ).fetchone()
        if row is None:
            return None
        return Workspace(
            workspace_id=str(row["workspace_id"]),
            root_path=Path(str(row["root_path"])),
            created_at=_time(row["created_at"]),
        )

    # -- Conversation ------------------------------------------------------

    def load_session(self, session_id: str) -> ConversationSession | None:
        self._ensure_schema()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM conversation_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        return self._session_from_row(row) if row is not None else None

    def list_sessions(
        self, *, limit: int = 20, cwd: str | None = None
    ) -> tuple[ConversationSession, ...]:
        if limit <= 0:
            return ()
        self._ensure_schema()
        query = "SELECT * FROM conversation_sessions"
        args: list[Any] = []
        if cwd is not None:
            query += " WHERE cwd = ?"
            args.append(str(Path(cwd).expanduser().resolve(strict=False)))
        query += " ORDER BY updated_at DESC, session_id ASC LIMIT ?"
        args.append(limit)
        with self._connect() as connection:
            rows = connection.execute(query, args).fetchall()
        return tuple(self._session_from_row(row) for row in rows)

    def list_runnable_session_ids(self) -> tuple[str, ...]:
        """返回启动恢复候选，不受历史查询的默认分页限制。"""
        self._ensure_schema()
        with self._connect() as connection:
            rows = connection.execute("""
                SELECT s.session_id
                FROM conversation_sessions AS s
                LEFT JOIN agent_run_states AS state
                  ON state.operation_id = s.active_operation_id
                WHERE s.archived_at IS NULL
                  AND (
                    state.status IN ('queued', 'running', 'cancelling')
                    OR (
                      s.active_operation_id IS NULL
                      AND EXISTS (
                        SELECT 1
                        FROM agent_inbox_messages AS message
                        WHERE message.session_id = s.session_id
                          AND message.status = 'pending'
                          AND message.delivery IN ('followup', 'steer')
                      )
                    )
                  )
                ORDER BY s.session_id ASC
                """).fetchall()
        return tuple(str(row["session_id"]) for row in rows)

    def append_node(
        self, *, node: ConversationNode, expected_node_id: str | None
    ) -> bool:
        """原子追加 Node，并同步移动 Session.active_node_id。"""
        self._ensure_schema()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            session = self._require_session(connection, node.session_id)
            if (
                session["archived_at"] is not None
                or session["active_operation_id"] is not None
                or session["active_node_id"] != expected_node_id
                or node.parent_node_id != expected_node_id
            ):
                connection.rollback()
                return False
            try:
                self._validate_node_artifacts(connection, node)
                connection.execute(
                    """
                    INSERT INTO conversation_nodes
                        (node_id, session_id, parent_node_id, content_type,
                         content_json, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        node.node_id,
                        node.session_id,
                        node.parent_node_id,
                        node.content_type,
                        node.content_json(),
                        node.created_at.isoformat(),
                    ),
                )
                cursor = connection.execute(
                    """
                    UPDATE conversation_sessions
                    SET active_node_id = ?, updated_at = ?
                    WHERE session_id = ? AND active_node_id IS ?
                      AND archived_at IS NULL AND active_operation_id IS NULL
                    """,
                    (
                        node.node_id,
                        node.created_at.isoformat(),
                        node.session_id,
                        expected_node_id,
                    ),
                )
                if cursor.rowcount != 1:
                    connection.rollback()
                    return False
            except sqlite3.IntegrityError as exc:
                raise StorageIntegrityError("ConversationNode 写入失败") from exc
        return True

    def load_node(self, node_id: str) -> ConversationNode | None:
        self._ensure_schema()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM conversation_nodes WHERE node_id = ?", (node_id,)
            ).fetchone()
        return self._node_from_row(row) if row is not None else None

    def list_branch_nodes(
        self, session_id: str, leaf_node_id: str | None
    ) -> tuple[ConversationNode, ...]:
        self._ensure_schema()
        if leaf_node_id is None:
            return ()
        with self._connect() as connection:
            rows = connection.execute(
                _LIST_BRANCH_NODES_SQL,
                (leaf_node_id, session_id, session_id),
            ).fetchall()
        return tuple(self._node_from_row(row) for row in rows)

    def move_active_node(
        self,
        *,
        session_id: str,
        expected_node_id: str | None,
        new_node_id: str | None,
        updated_at: datetime,
    ) -> bool:
        self._ensure_schema()
        with self._connect() as connection:
            if new_node_id is not None and not self._node_belongs(
                connection, new_node_id, session_id
            ):
                raise StorageIntegrityError("new_node_id 不存在或属于其他 Session")
            if expected_node_id is None:
                cursor = connection.execute(
                    """
                    UPDATE conversation_sessions
                    SET active_node_id = ?, updated_at = ?
                    WHERE session_id = ? AND active_node_id IS NULL
                      AND archived_at IS NULL AND active_operation_id IS NULL
                    """,
                    (new_node_id, updated_at.isoformat(), session_id),
                )
            else:
                cursor = connection.execute(
                    """
                    UPDATE conversation_sessions
                    SET active_node_id = ?, updated_at = ?
                    WHERE session_id = ? AND active_node_id = ?
                      AND archived_at IS NULL AND active_operation_id IS NULL
                    """,
                    (new_node_id, updated_at.isoformat(), session_id, expected_node_id),
                )
        return cursor.rowcount == 1

    def archive_session(self, *, session_id: str, archived_at: datetime) -> None:
        self._ensure_schema()
        with self._connect() as connection:
            existing = self._require_session(connection, session_id)
            if existing["archived_at"] is not None:
                return
            cursor = connection.execute(
                """
                UPDATE conversation_sessions
                SET archived_at = ?, updated_at = ?
                WHERE session_id = ? AND archived_at IS NULL
                  AND active_operation_id IS NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM agent_inbox_messages
                      WHERE session_id = ? AND status = 'pending'
                  )
                """,
                (
                    archived_at.isoformat(),
                    archived_at.isoformat(),
                    session_id,
                    session_id,
                ),
            )
            if cursor.rowcount != 1:
                self._require_session(connection, session_id)
                raise StorageIntegrityError("归档要求 Session 空闲且没有 pending Inbox")

    def unarchive_session(self, *, session_id: str, updated_at: datetime) -> None:
        self._ensure_schema()
        with self._connect() as connection:
            existing = self._require_session(connection, session_id)
            if existing["archived_at"] is None:
                return
            cursor = connection.execute(
                "UPDATE conversation_sessions SET archived_at = NULL, updated_at = ? WHERE session_id = ?",
                (updated_at.isoformat(), session_id),
            )
            if cursor.rowcount != 1:
                raise LookupError(f"ConversationSession 不存在: {session_id}")

    def delete_session(self, *, session_id: str) -> None:
        self._ensure_schema()
        with self._connect() as connection:
            self._check_deletable(connection, {session_id})
            if self._has_any_delegation(connection, {session_id}):
                raise StorageIntegrityError(
                    "公共 delete_session 要求不存在 AgentDelegation；请使用 delete_session_tree"
                )
            cursor = connection.execute(
                "DELETE FROM conversation_sessions WHERE session_id = ?", (session_id,)
            )
            if cursor.rowcount != 1:
                raise LookupError(f"ConversationSession 不存在: {session_id}")

    def delete_session_tree(self, *, session_id: str) -> None:
        self._ensure_schema()
        with self._connect() as connection:
            rows = connection.execute(
                """
                WITH RECURSIVE tree(session_id, depth) AS (
                    SELECT ?, 0
                    UNION
                    SELECT d.child_session_id, tree.depth + 1
                    FROM tree
                    JOIN session_operations op ON op.session_id = tree.session_id
                    JOIN agent_delegations d ON d.parent_operation_id = op.operation_id
                )
                SELECT session_id, MAX(depth) AS depth FROM tree GROUP BY session_id
                """,
                (session_id,),
            ).fetchall()
            ids = {str(row["session_id"]) for row in rows}
            if session_id not in ids:
                raise LookupError(f"ConversationSession 不存在: {session_id}")
            marks = ",".join("?" for _ in ids)
            external = connection.execute(
                f"""
                SELECT 1 FROM agent_delegations d
                JOIN session_operations op ON op.operation_id = d.parent_operation_id
                WHERE d.child_session_id IN ({marks}) AND op.session_id NOT IN ({marks})
                LIMIT 1
                """,
                [*ids, *ids],
            ).fetchone()
            if external is not None:
                raise StorageIntegrityError("子树根存在来自外部的 AgentDelegation")
            self._check_deletable(connection, ids)
            connection.execute(
                f"""
                DELETE FROM agent_delegations
                WHERE child_session_id IN ({marks})
                   OR parent_operation_id IN (
                       SELECT operation_id FROM session_operations
                       WHERE session_id IN ({marks})
                   )
                """,
                [*ids, *ids],
            )
            for child_id, _depth in sorted(
                ((str(row["session_id"]), int(row["depth"])) for row in rows),
                key=lambda item: item[1],
                reverse=True,
            ):
                connection.execute(
                    "DELETE FROM conversation_sessions WHERE session_id = ?",
                    (child_id,),
                )

    # -- Inbox --------------------------------------------------------------

    def send_message(
        self,
        *,
        message_id: str,
        session_id: str,
        delivery: MessageDelivery,
        message: UserMessage,
        source: MessageSource,
        created_at: datetime,
    ) -> InboxMessage:
        self._ensure_schema()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._require_active_session(connection, session_id)
                self._validate_message_artifacts(connection, message)
                sequence = int(
                    connection.execute(
                        "SELECT COALESCE(MAX(sequence), 0) + 1 FROM agent_inbox_messages WHERE session_id = ?",
                        (session_id,),
                    ).fetchone()[0]
                )
                inbox_message = InboxMessage(
                    message_id=message_id,
                    session_id=session_id,
                    sequence=sequence,
                    delivery=delivery,
                    message=message,
                    source=source,
                    created_at=created_at,
                )
                connection.execute(
                    """
                    INSERT INTO agent_inbox_messages (
                        message_id, session_id, sequence, delivery, message_json,
                        status, claimed_operation_id, claimed_step_id, outcome_reason,
                        created_at, handled_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        inbox_message.message_id,
                        inbox_message.session_id,
                        inbox_message.sequence,
                        inbox_message.delivery,
                        inbox_message.message_payload_json(),
                        inbox_message.status,
                        inbox_message.claimed_operation_id,
                        inbox_message.claimed_step_id,
                        inbox_message.outcome_reason,
                        inbox_message.created_at.isoformat(),
                        _iso(inbox_message.handled_at),
                    ),
                )
                return inbox_message
            except sqlite3.IntegrityError as exc:
                raise StorageIntegrityError("InboxMessage 写入失败") from exc

    def send_parent_followup(
        self,
        *,
        sender_operation_id: str,
        sender_step_id: str,
        sender_tool_call_id: str,
        target_child_session_id: str,
        message_id: str,
        message: UserMessage,
        source: AgentMessageSource,
        created_at: datetime,
    ) -> InboxMessage:
        """原子校验 sender ToolCall 并向 direct child 追加 followup。"""
        self._ensure_schema()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                operation_row = connection.execute(
                    "SELECT * FROM session_operations WHERE operation_id = ?",
                    (sender_operation_id,),
                ).fetchone()
                if operation_row is None:
                    raise StorageIntegrityError("sender Operation 不存在")
                sender_row = connection.execute(
                    "SELECT * FROM conversation_sessions WHERE session_id = ?",
                    (operation_row["session_id"],),
                ).fetchone()
                if sender_row is None:
                    raise StorageIntegrityError("sender Session 不存在")
                parent_row = connection.execute(
                    """
                    SELECT op.* FROM agent_delegations AS d
                    JOIN session_operations AS op
                      ON op.operation_id = d.parent_operation_id
                    WHERE d.child_session_id = ?
                    """,
                    (target_child_session_id,),
                ).fetchone()
                if (
                    parent_row is None
                    or parent_row["session_id"] != operation_row["session_id"]
                ):
                    raise StorageConflictError(
                        "target 不是 sender Session 的 direct child"
                    )
                target_row = connection.execute(
                    "SELECT * FROM conversation_sessions WHERE session_id = ?",
                    (target_child_session_id,),
                ).fetchone()
                if target_row is None:
                    raise StorageIntegrityError("target child Session 不存在")
                expected_source = AgentMessageSource(
                    sender_session_id=str(operation_row["session_id"]),
                    sender_operation_id=sender_operation_id,
                    form="followup",
                )
                if source != expected_source:
                    raise StorageIntegrityError("send_message source 不匹配")
                existing_row = connection.execute(
                    "SELECT * FROM agent_inbox_messages WHERE message_id = ?",
                    (message_id,),
                ).fetchone()
                if existing_row is not None:
                    existing = self._message_from_row(existing_row)
                    if (
                        existing.session_id != target_child_session_id
                        or existing.delivery != "followup"
                        or existing.message != message
                        or existing.source != source
                    ):
                        raise StorageConflictError("send_message 的稳定 ID 语义冲突")
                    connection.commit()
                    return existing
                if sender_row["active_operation_id"] != sender_operation_id:
                    raise StorageConflictError("sender Session 未指向 sender Operation")
                state_row = connection.execute(
                    "SELECT * FROM agent_run_states WHERE operation_id = ?",
                    (sender_operation_id,),
                ).fetchone()
                if state_row is None:
                    raise StorageIntegrityError("sender AgentRunState 不存在")
                state = self._run_state_from_row(state_row)
                if state.status != "running":
                    raise StorageConflictError("sender Operation 必须处于 running")
                step = state.current_step
                call = (
                    next(
                        (
                            item
                            for item in step.tool_calls
                            if item.tool_call_id == sender_tool_call_id
                        ),
                        None,
                    )
                    if step is not None
                    else None
                )
                expected_text = call.arguments.get("message") if call else None
                if (
                    step is None
                    or step.step_id != sender_step_id
                    or step.phase != "awaiting_tools"
                    or call is None
                    or call.tool_name != "send_message"
                    or call.status != "intent_recorded"
                    or call.execution_intent is not None
                    or call.arguments.get("child_session_id") != target_child_session_id
                    or not isinstance(expected_text, str)
                    or message != UserMessage((TextBlock(expected_text),))
                ):
                    raise StorageConflictError(
                        "sender ToolCall 不是当前 send_message intent_recorded"
                    )
                if target_row["archived_at"] is not None:
                    raise StorageConflictError("归档 child Session 不能接收新的消息")
                self._validate_message_artifacts(connection, message)
                sequence = int(
                    connection.execute(
                        """
                        SELECT COALESCE(MAX(sequence), 0) + 1
                        FROM agent_inbox_messages WHERE session_id = ?
                        """,
                        (target_child_session_id,),
                    ).fetchone()[0]
                )
                stored = InboxMessage(
                    message_id=message_id,
                    session_id=target_child_session_id,
                    sequence=sequence,
                    delivery="followup",
                    message=message,
                    source=source,
                    created_at=created_at,
                )
                connection.execute(
                    """
                    INSERT INTO agent_inbox_messages (
                        message_id, session_id, sequence, delivery, message_json,
                        status, claimed_operation_id, claimed_step_id, outcome_reason,
                        created_at, handled_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        stored.message_id,
                        stored.session_id,
                        stored.sequence,
                        stored.delivery,
                        stored.message_payload_json(),
                        stored.status,
                        stored.claimed_operation_id,
                        stored.claimed_step_id,
                        stored.outcome_reason,
                        stored.created_at.isoformat(),
                        None,
                    ),
                )
                connection.commit()
                return stored
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise StorageIntegrityError("send_message 写入失败") from exc
            except (StorageConflictError, StorageIntegrityError, ValueError, TypeError):
                connection.rollback()
                raise

    def send_child_report(
        self,
        *,
        sender_operation_id: str,
        sender_step_id: str,
        sender_tool_call_id: str,
        parent_session_id: str,
        message_id: str,
        message: UserMessage,
        source: AgentMessageSource,
        created_at: datetime,
    ) -> InboxMessage:
        """原子校验 child 直系父关系并追加 steer report。"""
        self._ensure_schema()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                operation_row = connection.execute(
                    "SELECT * FROM session_operations WHERE operation_id = ?",
                    (sender_operation_id,),
                ).fetchone()
                if operation_row is None:
                    raise StorageIntegrityError("sender Operation 不存在")
                sender_row = connection.execute(
                    "SELECT * FROM conversation_sessions WHERE session_id = ?",
                    (operation_row["session_id"],),
                ).fetchone()
                if sender_row is None:
                    raise StorageIntegrityError("sender Session 不存在")
                parent_row = connection.execute(
                    """
                    SELECT op.* FROM agent_delegations AS d
                    JOIN session_operations AS op
                      ON op.operation_id = d.parent_operation_id
                    WHERE d.child_session_id = ?
                    """,
                    (operation_row["session_id"],),
                ).fetchone()
                if parent_row is None:
                    raise StorageConflictError("只有 delegated child 才能 report")
                if parent_row["session_id"] != parent_session_id:
                    raise StorageIntegrityError("report parent Session 路由不匹配")
                parent_session_row = connection.execute(
                    "SELECT * FROM conversation_sessions WHERE session_id = ?",
                    (parent_session_id,),
                ).fetchone()
                if parent_session_row is None:
                    raise StorageIntegrityError("report 的 parent Session 不存在")
                expected_source = AgentMessageSource(
                    sender_session_id=str(operation_row["session_id"]),
                    sender_operation_id=sender_operation_id,
                    form="steer",
                )
                if source != expected_source:
                    raise StorageIntegrityError("report source 不匹配")
                existing_row = connection.execute(
                    "SELECT * FROM agent_inbox_messages WHERE message_id = ?",
                    (message_id,),
                ).fetchone()
                if existing_row is not None:
                    existing = self._message_from_row(existing_row)
                    if (
                        existing.session_id != parent_session_id
                        or existing.delivery != "steer"
                        or existing.message != message
                        or existing.source != source
                    ):
                        raise StorageConflictError("report 的稳定 ID 语义冲突")
                    connection.commit()
                    return existing
                if sender_row["active_operation_id"] != sender_operation_id:
                    raise StorageConflictError("sender Session 未指向 sender Operation")
                state_row = connection.execute(
                    "SELECT * FROM agent_run_states WHERE operation_id = ?",
                    (sender_operation_id,),
                ).fetchone()
                if state_row is None:
                    raise StorageIntegrityError("sender AgentRunState 不存在")
                state = self._run_state_from_row(state_row)
                step = state.current_step
                call = (
                    next(
                        (
                            item
                            for item in step.tool_calls
                            if item.tool_call_id == sender_tool_call_id
                        ),
                        None,
                    )
                    if step is not None
                    else None
                )
                expected_output = call.arguments.get("output") if call else None
                expected_message = (
                    UserMessage(
                        (
                            TextBlock(
                                f"Background subagent {operation_row['session_id']} reported:\n"
                                f"{expected_output}"
                            ),
                        )
                    )
                    if isinstance(expected_output, str)
                    else None
                )
                if (
                    step is None
                    or step.step_id != sender_step_id
                    or step.phase != "awaiting_tools"
                    or state.status != "running"
                    or call is None
                    or call.tool_name != "report"
                    or call.status != "intent_recorded"
                    or call.execution_intent is not None
                    or call.arguments != {"output": expected_output}
                    or expected_message != message
                ):
                    raise StorageConflictError(
                        "sender ToolCall 不是当前 report intent_recorded"
                    )
                if parent_session_row["archived_at"] is not None:
                    raise StorageConflictError(
                        "归档 parent Session 不能接收新的 report"
                    )
                self._validate_message_artifacts(connection, message)
                sequence = int(
                    connection.execute(
                        """
                        SELECT COALESCE(MAX(sequence), 0) + 1
                        FROM agent_inbox_messages WHERE session_id = ?
                        """,
                        (parent_session_id,),
                    ).fetchone()[0]
                )
                stored = InboxMessage(
                    message_id=message_id,
                    session_id=parent_session_id,
                    sequence=sequence,
                    delivery="steer",
                    message=message,
                    source=source,
                    created_at=created_at,
                )
                connection.execute(
                    """
                    INSERT INTO agent_inbox_messages (
                        message_id, session_id, sequence, delivery, message_json,
                        status, claimed_operation_id, claimed_step_id, outcome_reason,
                        created_at, handled_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        stored.message_id,
                        stored.session_id,
                        stored.sequence,
                        stored.delivery,
                        stored.message_payload_json(),
                        stored.status,
                        stored.claimed_operation_id,
                        stored.claimed_step_id,
                        stored.outcome_reason,
                        stored.created_at.isoformat(),
                        None,
                    ),
                )
                connection.commit()
                return stored
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise StorageIntegrityError("report 写入失败") from exc
            except (StorageConflictError, StorageIntegrityError, ValueError, TypeError):
                connection.rollback()
                raise

    def load_message(self, message_id: str) -> InboxMessage | None:
        self._ensure_schema()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM agent_inbox_messages WHERE message_id = ?", (message_id,)
            ).fetchone()
        return self._message_from_row(row) if row is not None else None

    def list_pending(
        self, *, session_id: str, delivery: str | None = None
    ) -> tuple[InboxMessage, ...]:
        self._ensure_schema()
        query = "SELECT * FROM agent_inbox_messages WHERE session_id = ? AND status = 'pending'"
        args: list[Any] = [session_id]
        if delivery is not None:
            query += " AND delivery = ?"
            args.append(delivery)
        query += " ORDER BY sequence ASC, message_id ASC"
        with self._connect() as connection:
            rows = connection.execute(query, args).fetchall()
        return tuple(self._message_from_row(row) for row in rows)

    def list_pending_step_messages(
        self, *, session_id: str
    ) -> tuple[InboxMessage, ...]:
        self._ensure_schema()
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM agent_inbox_messages
                WHERE session_id = ? AND status = 'pending'
                  AND delivery IN ('steer', 'inject')
                ORDER BY sequence ASC, message_id ASC
                """,
                (session_id,),
            ).fetchall()
        return tuple(self._message_from_row(row) for row in rows)

    def claim_message(
        self,
        *,
        message_id: str,
        operation_id: str,
        step_id: str | None,
        handled_at: datetime,
    ) -> bool:
        self._ensure_schema()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE agent_inbox_messages
                SET status = 'claimed', claimed_operation_id = ?, claimed_step_id = ?,
                    handled_at = ?
                WHERE message_id = ? AND status = 'pending'
                """,
                (operation_id, step_id, handled_at.isoformat(), message_id),
            )
        return cursor.rowcount == 1

    def discard_message(
        self, *, message_id: str, reason: str, handled_at: datetime
    ) -> bool:
        if not reason:
            raise ValueError("discard reason 不能为空")
        self._ensure_schema()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE agent_inbox_messages
                SET status = 'discarded', outcome_reason = ?, handled_at = ?
                WHERE message_id = ? AND status = 'pending'
                """,
                (reason, handled_at.isoformat(), message_id),
            )
        return cursor.rowcount == 1

    def discard_cancellation_messages(
        self, *, root_operation_id: str, reason: str, handled_at: datetime
    ) -> tuple[str, ...]:
        """丢弃取消祖先发往真实后代的 pending AgentMessage。"""
        if not reason:
            raise ValueError("取消消息丢弃原因不能为空")
        self._ensure_schema()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            graph = self._cancellation_graph(connection, root_operation_id)
            rows = connection.execute(
                "SELECT * FROM agent_inbox_messages WHERE status = 'pending'"
            ).fetchall()
            discarded: list[str] = []
            for row in rows:
                message = self._message_from_row(row)
                if not self._is_cancellation_message(connection, message, graph):
                    continue
                cursor = connection.execute(
                    """
                    UPDATE agent_inbox_messages
                    SET status = 'discarded', outcome_reason = ?, handled_at = ?
                    WHERE message_id = ? AND status = 'pending'
                    """,
                    (reason, handled_at.isoformat(), message.message_id),
                )
                if cursor.rowcount == 1:
                    discarded.append(message.message_id)
            connection.commit()
            return tuple(sorted(discarded))

    def cancellation_ready(self, *, root_operation_id: str) -> bool:
        self._ensure_schema()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            graph = self._cancellation_graph(connection, root_operation_id)
            for operation_id in graph["operation_ids"] - {root_operation_id}:
                row = connection.execute(
                    "SELECT status FROM agent_run_states WHERE operation_id = ?",
                    (operation_id,),
                ).fetchone()
                if row is None or str(row["status"]) not in {
                    "succeeded",
                    "failed",
                    "cancelled",
                }:
                    connection.rollback()
                    return False
            rows = connection.execute(
                "SELECT * FROM agent_inbox_messages WHERE status = 'pending'"
            ).fetchall()
            ready = not any(
                self._is_cancellation_message(
                    connection, self._message_from_row(row), graph
                )
                for row in rows
            )
            connection.rollback()
            return ready

    # -- Package / Artifact -------------------------------------------------

    def insert_agent_package_version(self, version: AgentPackageVersion) -> None:
        content = version.content_dict()
        expected_id = package_version_id_for_content(content)
        if version.package_version_id != expected_id:
            raise StorageIntegrityError("AgentPackageVersion ID 校验失败")
        self._ensure_schema()
        encoded = _json(content)
        try:
            with self._connect() as connection:
                existing = connection.execute(
                    "SELECT * FROM agent_package_versions WHERE package_version_id = ?",
                    (version.package_version_id,),
                ).fetchone()
                if existing is not None:
                    if (
                        str(existing["content_json"]) == encoded
                        and int(existing["format_version"]) == version.format_version
                    ):
                        return
                    raise StorageIntegrityError("AgentPackageVersion 内容冲突")
                connection.execute(
                    "INSERT INTO agent_package_versions VALUES (?, ?, ?, ?, ?)",
                    (
                        version.package_version_id,
                        version.agent_id,
                        version.format_version,
                        encoded,
                        version.created_at.isoformat(),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise StorageIntegrityError("AgentPackageVersion 写入失败") from exc

    def load_agent_package_version(
        self, package_version_id: str
    ) -> AgentPackageVersion | None:
        self._ensure_schema()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM agent_package_versions WHERE package_version_id = ?",
                (package_version_id,),
            ).fetchone()
        if row is None:
            return None
        if not _PACKAGE_ID.fullmatch(package_version_id):
            raise StorageIntegrityError("AgentPackageVersion ID 格式错误")
        content = _object(row["content_json"])
        if int(row["format_version"]) != content.get("format_version"):
            raise StorageIntegrityError("AgentPackageVersion format_version 不一致")
        if str(row["agent_id"]) != content.get("agent_id"):
            raise StorageIntegrityError("AgentPackageVersion agent_id 不一致")
        try:
            return decode_agent_package_content(
                package_version_id=package_version_id,
                content=content,
                created_at=_time(row["created_at"]),
            )
        except (TypeError, ValueError, KeyError) as exc:
            raise StorageIntegrityError("AgentPackageVersion 内容损坏") from exc

    def insert_artifact(self, artifact: Artifact) -> None:
        self._ensure_schema()
        try:
            with self._connect() as connection:
                existing = connection.execute(
                    "SELECT * FROM artifacts WHERE artifact_id = ?",
                    (artifact.artifact_id,),
                ).fetchone()
                if existing is not None:
                    if self._artifact_from_row(existing) == artifact:
                        return
                    raise StorageIntegrityError("Artifact ID 内容冲突")
                connection.execute(
                    "INSERT INTO artifacts VALUES (?, ?, ?)",
                    (
                        artifact.artifact_id,
                        artifact.size_bytes,
                        artifact.created_at.isoformat(),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise StorageIntegrityError("Artifact 写入失败") from exc

    def load_artifact(self, artifact_id: str) -> Artifact | None:
        self._ensure_schema()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM artifacts WHERE artifact_id = ?", (artifact_id,)
            ).fetchone()
        return self._artifact_from_row(row) if row is not None else None

    # -- Operation / RunState / Delegation --------------------------------

    def load_operation(self, operation_id: str) -> SessionOperation | None:
        self._ensure_schema()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM session_operations WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
        return self._operation_from_row(row) if row is not None else None

    def list_operations(self, *, session_id: str) -> tuple[SessionOperation, ...]:
        self._ensure_schema()
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM session_operations WHERE session_id = ? ORDER BY accepted_at, operation_id",
                (session_id,),
            ).fetchall()
        return tuple(self._operation_from_row(row) for row in rows)

    def load_run_state(self, operation_id: str) -> AgentRunState | None:
        self._ensure_schema()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM agent_run_states WHERE operation_id = ?", (operation_id,)
            ).fetchone()
        return self._run_state_from_row(row) if row is not None else None

    def commit_run_transition(
        self,
        *,
        state: AgentRunState,
        expected_revision: int,
        node: ConversationNode | None,
        updated_at: datetime,
    ) -> bool:
        """唯一 State CAS、可选 Node 与 Session 指针原子提交入口。"""
        self._ensure_schema()
        if state.revision != expected_revision + 1:
            raise StorageIntegrityError(
                "AgentRunState.revision 必须等于 expected_revision + 1"
            )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            session = connection.execute(
                """
                SELECT session_id, active_node_id, active_operation_id
                FROM conversation_sessions
                WHERE session_id = (
                    SELECT session_id FROM session_operations
                    WHERE operation_id = ?
                )
                """,
                (state.operation_id,),
            ).fetchone()
            if session is None or session["active_operation_id"] != state.operation_id:
                connection.rollback()
                return False
            current_state_row = connection.execute(
                "SELECT * FROM agent_run_states WHERE operation_id = ?",
                (state.operation_id,),
            ).fetchone()
            if current_state_row is None:
                connection.rollback()
                return False
            if self._transition_blocked_by_pending_step_messages(
                connection=connection,
                session_id=str(session["session_id"]),
                current_step=self._run_state_from_row(current_state_row).current_step,
                next_state=state,
            ):
                connection.rollback()
                return False
            if (
                str(current_state_row["status"]) == "cancelling"
                and state.status == "cancelled"
                and not self._cancellation_ready_in_connection(
                    connection, state.operation_id
                )
            ):
                connection.rollback()
                return False
            if node is not None:
                if node.session_id != str(session["session_id"]):
                    raise StorageIntegrityError(
                        "ConversationNode 不属于 Operation Session"
                    )
                if node.parent_node_id != _optional(session["active_node_id"]):
                    connection.rollback()
                    return False
                self._validate_node_artifacts(connection, node)
                if node.node_id not in _state_node_ids(state):
                    raise StorageIntegrityError(
                        "新 ConversationNode 必须被 AgentRunState 引用"
                    )
                try:
                    connection.execute(
                        "INSERT INTO conversation_nodes VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            node.node_id,
                            node.session_id,
                            node.parent_node_id,
                            node.content_type,
                            node.content_json(),
                            node.created_at.isoformat(),
                        ),
                    )
                except sqlite3.IntegrityError as exc:
                    raise StorageIntegrityError(
                        f"ConversationNode 写入失败: {node.node_id}"
                    ) from exc
            for node_id in _state_node_ids(state):
                if not self._node_belongs(
                    connection, node_id, str(session["session_id"])
                ):
                    raise StorageIntegrityError(
                        f"AgentRunState 引用的 Node 不属于 Operation Session: {node_id}"
                    )
            cursor = connection.execute(
                """
                UPDATE agent_run_states
                SET revision = ?, status = ?, waiting_reason = ?,
                    completed_step_count = ?, current_step_json = ?,
                    final_assistant_node_id = ?, error_json = ?,
                    cancellation_json = ?, updated_at = ?
                WHERE operation_id = ? AND revision = ?
                """,
                (
                    state.revision,
                    state.status,
                    state.waiting_reason,
                    state.completed_step_count,
                    _json(_step_to_dict(state.current_step)),
                    state.final_assistant_node_id,
                    _json(
                        {
                            "code": state.error.code,
                            "message": state.error.message,
                            "retryable": state.error.retryable,
                        }
                        if state.error is not None
                        else None
                    ),
                    _json(
                        {
                            "cause": state.cancellation.cause,
                            "requested_at": state.cancellation.requested_at.isoformat(),
                        }
                        if state.cancellation is not None
                        else None
                    ),
                    updated_at.isoformat(),
                    state.operation_id,
                    expected_revision,
                ),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                return False
            connection.execute(
                """
                UPDATE conversation_sessions
                SET active_node_id = COALESCE(?, active_node_id),
                    active_operation_id = CASE WHEN ? THEN NULL ELSE active_operation_id END,
                    updated_at = ?
                WHERE session_id = ? AND active_operation_id = ?
                """,
                (
                    node.node_id if node is not None else None,
                    state.status in {"succeeded", "failed", "cancelled"},
                    updated_at.isoformat(),
                    session["session_id"],
                    state.operation_id,
                ),
            )
        return True

    def claim_step_messages(
        self,
        *,
        message_ids: tuple[str, ...],
        state: AgentRunState,
        expected_revision: int,
        updated_at: datetime,
    ) -> bool:
        self._ensure_schema()
        if not message_ids or state.revision != expected_revision + 1:
            return False
        if len(set(message_ids)) != len(message_ids):
            return False
        step = state.current_step
        if step is None or step.phase != "preparing_request":
            return False
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                current = connection.execute(
                    "SELECT * FROM agent_run_states WHERE operation_id = ?",
                    (state.operation_id,),
                ).fetchone()
                operation = connection.execute(
                    "SELECT session_id FROM session_operations WHERE operation_id = ?",
                    (state.operation_id,),
                ).fetchone()
                if current is None or operation is None:
                    connection.rollback()
                    return False
                session = self._require_session(
                    connection, str(operation["session_id"])
                )
                if (
                    int(current["revision"]) != expected_revision
                    or session["archived_at"] is not None
                    or session["active_operation_id"] != state.operation_id
                    or state.status != "running"
                ):
                    connection.rollback()
                    return False
                messages: list[sqlite3.Row] = []
                for message_id in message_ids:
                    message = connection.execute(
                        "SELECT * FROM agent_inbox_messages WHERE message_id = ?",
                        (message_id,),
                    ).fetchone()
                    if (
                        message is None
                        or str(message["session_id"]) != str(operation["session_id"])
                        or message["status"] != "pending"
                        or message["delivery"] not in {"steer", "inject"}
                        or connection.execute(
                            "SELECT 1 FROM conversation_nodes WHERE node_id = ?",
                            (message_id,),
                        ).fetchone()
                        is not None
                    ):
                        connection.rollback()
                        return False
                    messages.append(message)
                if [int(row["sequence"]) for row in messages] != sorted(
                    int(row["sequence"]) for row in messages
                ):
                    connection.rollback()
                    return False
                parent_node_id = _optional(session["active_node_id"])
                nodes: list[ConversationNode] = []
                for row in messages:
                    node = self._message_to_node(row, parent_node_id=parent_node_id)
                    self._validate_node_artifacts(connection, node)
                    nodes.append(node)
                    parent_node_id = node.node_id
                for node in nodes:
                    connection.execute(
                        "INSERT INTO conversation_nodes VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            node.node_id,
                            node.session_id,
                            node.parent_node_id,
                            node.content_type,
                            node.content_json(),
                            node.created_at.isoformat(),
                        ),
                    )
                for message in messages:
                    cursor = connection.execute(
                        """
                        UPDATE agent_inbox_messages
                        SET status = 'claimed', claimed_operation_id = ?,
                            claimed_step_id = ?, handled_at = ?
                        WHERE message_id = ? AND status = 'pending'
                        """,
                        (
                            state.operation_id,
                            step.step_id,
                            updated_at.isoformat(),
                            message["message_id"],
                        ),
                    )
                    if cursor.rowcount != 1:
                        connection.rollback()
                        return False
                cursor = connection.execute(
                    """
                    UPDATE agent_run_states
                    SET revision = ?, status = ?, waiting_reason = ?,
                        completed_step_count = ?, current_step_json = ?,
                        final_assistant_node_id = ?, error_json = ?,
                        cancellation_json = ?, updated_at = ?
                    WHERE operation_id = ? AND revision = ?
                    """,
                    (
                        *self._run_state_values(state, updated_at=updated_at)[1:],
                        state.operation_id,
                        expected_revision,
                    ),
                )
                if cursor.rowcount != 1:
                    connection.rollback()
                    return False
                cursor = connection.execute(
                    """
                    UPDATE conversation_sessions
                    SET active_node_id = ?, updated_at = ?
                    WHERE session_id = ? AND active_operation_id = ?
                    """,
                    (
                        nodes[-1].node_id,
                        updated_at.isoformat(),
                        operation["session_id"],
                        state.operation_id,
                    ),
                )
                if cursor.rowcount != 1:
                    connection.rollback()
                    return False
            except (
                sqlite3.IntegrityError,
                StorageIntegrityError,
                ValueError,
                TypeError,
            ):
                connection.rollback()
                return False
        return True

    def load_delegation(self, child_session_id: str) -> AgentDelegation | None:
        self._ensure_schema()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM agent_delegations WHERE child_session_id = ?",
                (child_session_id,),
            ).fetchone()
        return self._delegation_from_row(row) if row is not None else None

    def insert_delegation(self, delegation: AgentDelegation) -> None:
        self._ensure_schema()
        try:
            with self._connect() as connection:
                connection.execute(
                    "INSERT INTO agent_delegations VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        delegation.child_session_id,
                        delegation.parent_operation_id,
                        delegation.parent_step_id,
                        delegation.parent_tool_call_id,
                        delegation.initial_message_id,
                        delegation.created_at.isoformat(),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise StorageIntegrityError("AgentDelegation 写入失败") from exc

    def start_delegation(
        self,
        *,
        parent_operation_id: str,
        parent_step_id: str,
        parent_tool_call_id: str,
        child_session: ConversationSession,
        delegation: AgentDelegation,
        message_id: str,
        message: UserMessage,
        source: AgentMessageSource,
        created_at: datetime,
    ) -> AgentDelegation:
        """在一个事务内创建 child Session、Inbox 和 Delegation。"""
        self._ensure_schema()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                operation_row = connection.execute(
                    "SELECT * FROM session_operations WHERE operation_id = ?",
                    (parent_operation_id,),
                ).fetchone()
                state_row = connection.execute(
                    "SELECT * FROM agent_run_states WHERE operation_id = ?",
                    (parent_operation_id,),
                ).fetchone()
                if operation_row is None or state_row is None:
                    raise StorageIntegrityError("parent Operation/State 不存在")
                parent_row = connection.execute(
                    "SELECT * FROM conversation_sessions WHERE session_id = ?",
                    (operation_row["session_id"],),
                ).fetchone()
                if parent_row is None:
                    raise StorageIntegrityError("parent Session 不存在")
                workspace_row = connection.execute(
                    "SELECT 1 FROM workspaces WHERE workspace_id = ?",
                    (parent_row["workspace_id"],),
                ).fetchone()
                if workspace_row is None:
                    raise StorageIntegrityError("parent Workspace 不存在")
                state = self._run_state_from_row(state_row)
                if parent_row["active_operation_id"] != parent_operation_id:
                    raise StorageConflictError("parent Session 未指向 parent Operation")
                if state.status != "running":
                    raise StorageConflictError("parent Operation 必须处于 running")
                operation = self._operation_from_row(operation_row)
                if operation.workspace_binding.workspace_id != parent_row[
                    "workspace_id"
                ] or operation.workspace_binding.working_directory != Path(
                    str(parent_row["cwd"])
                ):
                    raise StorageIntegrityError(
                        "parent Operation.workspace_binding 漂移"
                    )
                step = state.current_step
                call = (
                    next(
                        (
                            item
                            for item in step.tool_calls
                            if item.tool_call_id == parent_tool_call_id
                        ),
                        None,
                    )
                    if step is not None
                    else None
                )
                if (
                    step is None
                    or step.step_id != parent_step_id
                    or step.phase != "awaiting_tools"
                    or call is None
                    or call.status != "intent_recorded"
                    or not isinstance(call.execution_intent, DelegateAgentIntent)
                ):
                    raise StorageConflictError(
                        "parent ToolCall 不满足 delegation acceptance"
                    )
                parent_package = self._load_package_in_transaction(
                    connection, str(operation_row["agent_package_version_id"])
                )
                child_package = self._load_package_in_transaction(
                    connection, call.execution_intent.child_package_version_id
                )
                if parent_package is None or child_package is None:
                    raise StorageIntegrityError(
                        "parent/child AgentPackageVersion 不存在"
                    )
                depth = self._delegation_depth_in_transaction(
                    connection, str(operation_row["session_id"])
                )
                if depth >= parent_package.runtime_policy.max_delegation_depth:
                    raise StorageConflictError("已达到最大 delegation depth")
                expected_source = AgentMessageSource(
                    sender_session_id=str(operation_row["session_id"]),
                    sender_operation_id=parent_operation_id,
                    form="followup",
                )
                self._validate_delegation_request(
                    operation_id=parent_operation_id,
                    parent_row=parent_row,
                    child_package=child_package,
                    child_session=child_session,
                    delegation=delegation,
                    message_id=message_id,
                    parent_step_id=parent_step_id,
                    parent_tool_call_id=parent_tool_call_id,
                    message=message,
                    source=source,
                    expected_source=expected_source,
                )
                existing_row = connection.execute(
                    "SELECT * FROM agent_delegations WHERE parent_tool_call_id = ?",
                    (parent_tool_call_id,),
                ).fetchone()
                if existing_row is not None:
                    existing = self._delegation_from_row(existing_row)
                    existing_session = connection.execute(
                        "SELECT * FROM conversation_sessions WHERE session_id = ?",
                        (existing.child_session_id,),
                    ).fetchone()
                    existing_message_row = connection.execute(
                        "SELECT * FROM agent_inbox_messages WHERE message_id = ?",
                        (existing.initial_message_id,),
                    ).fetchone()
                    existing_message = (
                        self._message_from_row(existing_message_row)
                        if existing_message_row is not None
                        else None
                    )
                    if (
                        existing_session is None
                        or existing_message is None
                        or existing.parent_operation_id != parent_operation_id
                        or existing.parent_step_id != parent_step_id
                        or existing.parent_tool_call_id != parent_tool_call_id
                        or str(existing_session["agent_id"]) != child_package.agent_id
                        or str(existing_session["workspace_id"])
                        != str(parent_row["workspace_id"])
                        or Path(str(existing_session["cwd"]))
                        != Path(str(parent_row["cwd"]))
                        or existing_message.message != message
                        or existing_message.source != source
                    ):
                        raise StorageConflictError(
                            "同一 parent ToolCall 的 delegation 请求语义冲突"
                        )
                    connection.commit()
                    return existing
                if (
                    connection.execute(
                        "SELECT 1 FROM conversation_sessions WHERE session_id = ?",
                        (child_session.session_id,),
                    ).fetchone()
                    is not None
                ):
                    raise StorageConflictError("child Session ID 已存在")
                if (
                    connection.execute(
                        "SELECT 1 FROM agent_inbox_messages WHERE message_id = ?",
                        (message_id,),
                    ).fetchone()
                    is not None
                ):
                    raise StorageConflictError("initial InboxMessage ID 已存在")
                sequence = int(
                    connection.execute(
                        "SELECT COALESCE(MAX(sequence), 0) + 1 FROM agent_inbox_messages WHERE session_id = ?",
                        (child_session.session_id,),
                    ).fetchone()[0]
                )
                inbox_message = InboxMessage(
                    message_id=message_id,
                    session_id=child_session.session_id,
                    sequence=sequence,
                    delivery="followup",
                    message=message,
                    source=source,
                    created_at=created_at,
                )
                self._validate_message_artifacts(connection, message)
                connection.execute(
                    "INSERT INTO conversation_sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        child_session.session_id,
                        child_session.agent_id,
                        child_session.workspace_id,
                        str(child_session.cwd),
                        None,
                        None,
                        None,
                        None,
                        child_session.created_at.isoformat(),
                        child_session.updated_at.isoformat(),
                        None,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO agent_inbox_messages (
                        message_id, session_id, sequence, delivery, message_json,
                        status, claimed_operation_id, claimed_step_id, outcome_reason,
                        created_at, handled_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        inbox_message.message_id,
                        inbox_message.session_id,
                        inbox_message.sequence,
                        inbox_message.delivery,
                        inbox_message.message_payload_json(),
                        "pending",
                        None,
                        None,
                        None,
                        inbox_message.created_at.isoformat(),
                        None,
                    ),
                )
                connection.execute(
                    "INSERT INTO agent_delegations VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        delegation.child_session_id,
                        delegation.parent_operation_id,
                        delegation.parent_step_id,
                        delegation.parent_tool_call_id,
                        delegation.initial_message_id,
                        delegation.created_at.isoformat(),
                    ),
                )
                connection.commit()
                return delegation
            except (
                sqlite3.IntegrityError,
                StorageConflictError,
                StorageIntegrityError,
                ValueError,
                TypeError,
            ):
                connection.rollback()
                raise

    @staticmethod
    def _load_package_in_transaction(connection, package_id):
        row = connection.execute(
            "SELECT * FROM agent_package_versions WHERE package_version_id = ?",
            (package_id,),
        ).fetchone()
        if row is None:
            return None
        return decode_agent_package_content(
            package_version_id=package_id,
            content=_object(row["content_json"]),
            created_at=_time(row["created_at"]),
        )

    @staticmethod
    def _delegation_depth_in_transaction(connection, session_id: str) -> int:
        depth = 0
        seen: set[str] = set()
        while session_id not in seen:
            seen.add(session_id)
            row = connection.execute(
                "SELECT parent_operation_id FROM agent_delegations WHERE child_session_id = ?",
                (session_id,),
            ).fetchone()
            if row is None:
                return depth
            depth += 1
            operation = connection.execute(
                "SELECT session_id FROM session_operations WHERE operation_id = ?",
                (row["parent_operation_id"],),
            ).fetchone()
            if operation is None:
                raise StorageIntegrityError("delegation parent Operation 不存在")
            session_id = str(operation["session_id"])
        raise StorageIntegrityError("delegation parent 链存在环")

    @staticmethod
    def _validate_delegation_request(
        *,
        operation_id,
        parent_row,
        child_package,
        child_session,
        delegation,
        message_id,
        parent_step_id,
        parent_tool_call_id,
        message,
        source,
        expected_source,
    ) -> None:
        del message
        if delegation.parent_operation_id != operation_id:
            raise StorageIntegrityError("delegation parent_operation_id 不匹配")
        if delegation.child_session_id != child_session.session_id:
            raise StorageIntegrityError("delegation child_session_id 不匹配")
        if (
            delegation.parent_step_id != parent_step_id
            or delegation.parent_tool_call_id != parent_tool_call_id
            or delegation.initial_message_id != message_id
        ):
            raise StorageIntegrityError("delegation 身份字段不匹配")
        if source != expected_source:
            raise StorageIntegrityError("initial InboxMessage source 不匹配")
        if child_session.agent_id != child_package.agent_id:
            raise StorageIntegrityError("child Session.agent_id 不匹配 child Package")
        if (
            child_session.workspace_id != parent_row["workspace_id"]
            or child_session.cwd != Path(str(parent_row["cwd"]))
            or any(
                value is not None
                for value in (
                    child_session.active_node_id,
                    child_session.active_operation_id,
                    child_session.title,
                    child_session.title_source,
                    child_session.archived_at,
                )
            )
        ):
            raise StorageIntegrityError("child Session 不是继承 Workspace 的空 Session")

    def find_delegation_by_parent_tool_call(
        self, parent_tool_call_id: str
    ) -> AgentDelegation | None:
        self._ensure_schema()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM agent_delegations WHERE parent_tool_call_id = ?",
                (parent_tool_call_id,),
            ).fetchone()
        return self._delegation_from_row(row) if row is not None else None

    def list_delegations(
        self, *, parent_operation_id: str
    ) -> tuple[AgentDelegation, ...]:
        self._ensure_schema()
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM agent_delegations WHERE parent_operation_id = ? ORDER BY created_at, child_session_id",
                (parent_operation_id,),
            ).fetchall()
        return tuple(self._delegation_from_row(row) for row in rows)

    def accept_operation(
        self,
        *,
        operation: SessionOperation,
        state: AgentRunState,
        expected_node_id: str | None,
    ) -> bool:
        """原子执行 11.3：claim Inbox、插入输入 Node、Operation 和 State。"""
        self._ensure_schema()
        session_id = operation.session_id
        if state.operation_id != operation.operation_id:
            raise StorageIntegrityError("Operation 与 State 身份不一致")
        if state.revision != 1 or state.status != "queued":
            raise StorageIntegrityError("接受 Operation 必须从 queued/revision=1 开始")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            session = self._require_session(connection, session_id)
            if (
                session["archived_at"] is not None
                or session["active_operation_id"] is not None
            ):
                connection.rollback()
                return False
            if session["active_node_id"] != expected_node_id:
                connection.rollback()
                return False
            message = connection.execute(
                "SELECT * FROM agent_inbox_messages WHERE message_id = ? AND session_id = ? AND status = 'pending'",
                (operation.input_node_id, session_id),
            ).fetchone()
            if message is None:
                connection.rollback()
                return False
            self._validate_operation_refs(connection, operation)
            node = self._message_to_node(message, parent_node_id=expected_node_id)
            self._validate_node_artifacts(connection, node)
            connection.execute(
                "INSERT INTO conversation_nodes VALUES (?, ?, ?, ?, ?, ?)",
                (
                    node.node_id,
                    node.session_id,
                    node.parent_node_id,
                    node.content_type,
                    node.content_json(),
                    node.created_at.isoformat(),
                ),
            )
            connection.execute(
                "INSERT INTO session_operations VALUES (?, ?, ?, ?, ?, ?)",
                (
                    operation.operation_id,
                    operation.session_id,
                    operation.agent_package_version_id,
                    self._binding_json(operation),
                    operation.input_node_id,
                    operation.accepted_at.isoformat(),
                ),
            )
            connection.execute(
                "INSERT INTO agent_run_states VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                self._run_state_values(state, updated_at=operation.accepted_at),
            )
            connection.execute(
                "UPDATE conversation_sessions SET active_node_id = ?, active_operation_id = ?, updated_at = ? WHERE session_id = ?",
                (
                    node.node_id,
                    operation.operation_id,
                    operation.accepted_at.isoformat(),
                    session_id,
                ),
            )
            connection.execute(
                "UPDATE agent_inbox_messages SET status = 'claimed', claimed_operation_id = ?, handled_at = ? WHERE message_id = ?",
                (
                    operation.operation_id,
                    operation.accepted_at.isoformat(),
                    operation.input_node_id,
                ),
            )
        return True

    # -- Internal ----------------------------------------------------------

    def _ensure_schema(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version == 0:
                create_schema(connection)
            elif version == 9:
                raise UnsupportedStorageSchemaError(
                    "检测到 SQLite schema version 9；请先执行一次性 v9→v10 迁移"
                )
            elif version != SCHEMA_VERSION:
                raise UnsupportedSchemaVersionError(
                    f"不支持的 SQLite schema version: {version}"
                )

    def _connect(self) -> sqlite3.Connection:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self._db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @staticmethod
    def _session_from_row(row: sqlite3.Row) -> ConversationSession:
        return ConversationSession(
            session_id=str(row["session_id"]),
            agent_id=str(row["agent_id"]),
            workspace_id=str(row["workspace_id"]),
            cwd=Path(str(row["cwd"])),
            active_node_id=_optional(row["active_node_id"]),
            active_operation_id=_optional(row["active_operation_id"]),
            title=_optional(row["title"]),
            title_source=_optional(row["title_source"]),
            created_at=_time(row["created_at"]),
            updated_at=_time(row["updated_at"]),
            archived_at=_optional_time(row["archived_at"]),
        )

    @staticmethod
    def _node_from_row(row: sqlite3.Row) -> ConversationNode:
        return ConversationNode.from_content_json(
            node_id=str(row["node_id"]),
            session_id=str(row["session_id"]),
            parent_node_id=_optional(row["parent_node_id"]),
            content_type=str(row["content_type"]),
            content_json=str(row["content_json"]),
            created_at=_time(row["created_at"]),
        )  # type: ignore[arg-type]

    @staticmethod
    def _message_from_row(row: sqlite3.Row) -> InboxMessage:
        payload = _object(row["message_json"])
        value = {
            "message_id": row["message_id"],
            "session_id": row["session_id"],
            "sequence": row["sequence"],
            "delivery": row["delivery"],
            "message": payload["message"],
            "source": payload["source"],
            "status": row["status"],
            "claimed_operation_id": row["claimed_operation_id"],
            "claimed_step_id": row["claimed_step_id"],
            "outcome_reason": row["outcome_reason"],
            "created_at": row["created_at"],
            "handled_at": row["handled_at"],
        }
        return InboxMessage.from_json(_json(value) or "{}")

    @staticmethod
    def _operation_from_row(row: sqlite3.Row) -> SessionOperation:
        from pickel.workspaces.workspace_binding import WorkspaceBinding

        binding = _object(row["workspace_binding_json"])
        return SessionOperation(
            operation_id=str(row["operation_id"]),
            session_id=str(row["session_id"]),
            agent_package_version_id=str(row["agent_package_version_id"]),
            workspace_binding=WorkspaceBinding(
                workspace_id=str(binding["workspace_id"]),
                working_directory=Path(str(binding["working_directory"])),
                allowed_root=(
                    Path(str(binding["allowed_root"]))
                    if binding.get("allowed_root") is not None
                    else None
                ),
            ),
            input_node_id=str(row["input_node_id"]),
            accepted_at=_time(row["accepted_at"]),
        )

    @staticmethod
    def _run_state_from_row(row: sqlite3.Row) -> AgentRunState:
        content = {
            "operation_id": row["operation_id"],
            "revision": row["revision"],
            "status": row["status"],
            "waiting_reason": row["waiting_reason"],
            "completed_step_count": row["completed_step_count"],
            "current_step": (
                _object(row["current_step_json"])
                if row["current_step_json"] is not None
                else None
            ),
            "final_assistant_node_id": row["final_assistant_node_id"],
            "error": _object(row["error_json"]) if row["error_json"] else None,
            "cancellation": (
                _object(row["cancellation_json"]) if row["cancellation_json"] else None
            ),
        }
        return agent_run_state_from_content(content)

    @staticmethod
    def _delegation_from_row(row: sqlite3.Row) -> AgentDelegation:
        return AgentDelegation(
            child_session_id=str(row["child_session_id"]),
            parent_operation_id=str(row["parent_operation_id"]),
            parent_step_id=str(row["parent_step_id"]),
            parent_tool_call_id=str(row["parent_tool_call_id"]),
            initial_message_id=str(row["initial_message_id"]),
            created_at=_time(row["created_at"]),
        )

    @staticmethod
    def _artifact_from_row(row: sqlite3.Row) -> Artifact:
        return Artifact(
            artifact_id=str(row["artifact_id"]),
            size_bytes=int(row["size_bytes"]),
            created_at=_time(row["created_at"]),
        )

    @staticmethod
    def _node_belongs(
        connection: sqlite3.Connection, node_id: str, session_id: str
    ) -> bool:
        return (
            connection.execute(
                "SELECT 1 FROM conversation_nodes WHERE node_id = ? AND session_id = ?",
                (node_id, session_id),
            ).fetchone()
            is not None
        )

    @staticmethod
    def _validate_node_artifacts(
        connection: sqlite3.Connection, node: ConversationNode
    ) -> None:
        SQLiteRuntimeStore._validate_message_artifacts(connection, node.content)

    @staticmethod
    def _transition_blocked_by_pending_step_messages(
        *,
        connection: sqlite3.Connection,
        session_id: str,
        current_step: Any,
        next_state: AgentRunState,
    ) -> bool:
        pending = connection.execute(
            """
            SELECT 1 FROM agent_inbox_messages
            WHERE session_id = ? AND status = 'pending'
              AND delivery IN ('steer', 'inject')
            LIMIT 1
            """,
            (session_id,),
        ).fetchone()
        next_step = next_state.current_step
        intent_or_terminal = (
            current_step is not None
            and next_step is not None
            and current_step.phase == "preparing_request"
            and next_step.phase == "request_ready"
        ) or next_state.status == "succeeded"
        has_pending = pending is not None
        if intent_or_terminal:
            return has_pending
        if (
            current_step is not None
            and current_step.phase == "awaiting_tools"
            and not current_step.tool_calls
            and next_state.status == "running"
            and next_step is None
        ):
            return not has_pending
        return False

    @staticmethod
    def _cancellation_graph(
        connection: sqlite3.Connection, root_operation_id: str
    ) -> dict[str, object]:
        """沿不可变 Delegation 关系返回取消所需的窄图投影。"""
        operation_ids: set[str] = {root_operation_id}
        descendants_by_operation: dict[str, set[str]] = {}
        visited: set[str] = set()

        def visit(operation_id: str) -> None:
            if operation_id in visited:
                return
            visited.add(operation_id)
            rows = connection.execute(
                """
                SELECT child_session_id FROM agent_delegations
                WHERE parent_operation_id = ?
                """,
                (operation_id,),
            ).fetchall()
            for row in rows:
                child_session_id = str(row["child_session_id"])
                descendants = descendants_by_operation.setdefault(operation_id, set())
                descendants.add(child_session_id)
                child_rows = connection.execute(
                    """
                    SELECT operation_id FROM session_operations
                    WHERE session_id = ?
                    """,
                    (child_session_id,),
                ).fetchall()
                for child_row in child_rows:
                    child_operation_id = str(child_row["operation_id"])
                    operation_ids.add(child_operation_id)
                    visit(child_operation_id)
                    descendants.update(
                        descendants_by_operation.get(child_operation_id, ())
                    )

        visit(root_operation_id)
        return {
            "operation_ids": operation_ids,
            "descendants_by_operation": descendants_by_operation,
        }

    @staticmethod
    def _is_cancellation_message(
        connection: sqlite3.Connection,
        message: InboxMessage,
        graph: dict[str, object],
    ) -> bool:
        source = message.source
        if not isinstance(source, AgentMessageSource):
            return False
        descendants_by_operation = graph["descendants_by_operation"]
        if not isinstance(descendants_by_operation, dict):
            return False
        target_sessions = descendants_by_operation.get(source.sender_operation_id)
        sender = connection.execute(
            "SELECT session_id FROM session_operations WHERE operation_id = ?",
            (source.sender_operation_id,),
        ).fetchone()
        return (
            sender is not None
            and str(sender["session_id"]) == source.sender_session_id
            and source.form == message.delivery
            and isinstance(target_sessions, set)
            and message.session_id in target_sessions
        )

    @classmethod
    def _cancellation_ready_in_connection(
        cls, connection: sqlite3.Connection, root_operation_id: str
    ) -> bool:
        graph = cls._cancellation_graph(connection, root_operation_id)
        for operation_id in graph["operation_ids"] - {root_operation_id}:
            row = connection.execute(
                "SELECT status FROM agent_run_states WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            if row is None or str(row["status"]) not in {
                "succeeded",
                "failed",
                "cancelled",
            }:
                return False
        rows = connection.execute(
            "SELECT * FROM agent_inbox_messages WHERE status = 'pending'"
        ).fetchall()
        return not any(
            cls._is_cancellation_message(connection, cls._message_from_row(row), graph)
            for row in rows
        )

    @staticmethod
    def _validate_message_artifacts(
        connection: sqlite3.Connection, message: UserMessage
    ) -> None:
        for block in message.content:
            if isinstance(block, ArtifactBlock):
                exists = connection.execute(
                    "SELECT 1 FROM artifacts WHERE artifact_id = ?",
                    (block.artifact.artifact_id,),
                ).fetchone()
                if exists is None:
                    raise StorageIntegrityError(
                        "ConversationNode 引用不存在的 Artifact: "
                        f"{block.artifact.artifact_id}"
                    )

    @staticmethod
    def _require_session(
        connection: sqlite3.Connection, session_id: str
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM conversation_sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        if row is None:
            raise LookupError(f"ConversationSession 不存在: {session_id}")
        return row

    @classmethod
    def _require_active_session(
        cls, connection: sqlite3.Connection, session_id: str
    ) -> sqlite3.Row:
        row = cls._require_session(connection, session_id)
        if row["archived_at"] is not None:
            raise StorageIntegrityError("归档 Session 不能接受新的 InboxMessage")
        return row

    @classmethod
    def _validate_operation_refs(
        cls, connection: sqlite3.Connection, operation: SessionOperation
    ) -> None:
        session = cls._require_active_session(connection, operation.session_id)
        package = connection.execute(
            "SELECT agent_id FROM agent_package_versions WHERE package_version_id = ?",
            (operation.agent_package_version_id,),
        ).fetchone()
        if package is None:
            raise StorageIntegrityError("AgentPackageVersion 不存在")
        if str(package["agent_id"]) != str(session["agent_id"]):
            raise StorageIntegrityError(
                "AgentPackageVersion.agent_id 与 Session 不匹配"
            )
        if not cls._node_belongs(
            connection, operation.input_node_id, operation.session_id
        ):
            pending = connection.execute(
                "SELECT 1 FROM agent_inbox_messages WHERE message_id = ? AND session_id = ? AND status = 'pending'",
                (operation.input_node_id, operation.session_id),
            ).fetchone()
            if pending is None:
                raise StorageIntegrityError("input_node_id 不存在或不属于 Session")
        if operation.workspace_binding.workspace_id != str(session["workspace_id"]):
            raise StorageIntegrityError(
                "WorkspaceBinding 与 Session.workspace_id 不匹配"
            )

    @staticmethod
    def _binding_json(operation: SessionOperation) -> str:
        binding = operation.workspace_binding
        return (
            _json(
                {
                    "workspace_id": binding.workspace_id,
                    "working_directory": str(binding.working_directory),
                    "allowed_root": (
                        str(binding.allowed_root) if binding.allowed_root else None
                    ),
                }
            )
            or "{}"
        )

    @staticmethod
    def _run_state_values(
        state: AgentRunState, *, updated_at: datetime
    ) -> tuple[Any, ...]:
        return (
            state.operation_id,
            state.revision,
            state.status,
            state.waiting_reason,
            state.completed_step_count,
            _json(_step_to_dict(state.current_step)),
            state.final_assistant_node_id,
            _json(
                {
                    "code": state.error.code,
                    "message": state.error.message,
                    "retryable": state.error.retryable,
                }
                if state.error is not None
                else None
            ),
            _json(
                {
                    "cause": state.cancellation.cause,
                    "requested_at": state.cancellation.requested_at.isoformat(),
                }
                if state.cancellation is not None
                else None
            ),
            updated_at.isoformat(),
        )

    @staticmethod
    def _message_to_node(
        row: sqlite3.Row, *, parent_node_id: str | None
    ) -> ConversationNode:
        payload = _object(row["message_json"])
        return ConversationNode.from_content_json(
            node_id=str(row["message_id"]),
            session_id=str(row["session_id"]),
            parent_node_id=parent_node_id,
            content_type="agent_message",
            content_json=_json(payload["message"]) or "{}",
            created_at=_time(row["created_at"]),
        )

    @staticmethod
    def _has_any_delegation(
        connection: sqlite3.Connection, session_ids: set[str]
    ) -> bool:
        if not session_ids:
            return False
        marks = ",".join("?" for _ in session_ids)
        return (
            connection.execute(
                f"""
            SELECT 1 FROM agent_delegations d
            LEFT JOIN session_operations op ON op.operation_id = d.parent_operation_id
            WHERE d.child_session_id IN ({marks}) OR op.session_id IN ({marks}) LIMIT 1
            """,
                [*session_ids, *session_ids],
            ).fetchone()
            is not None
        )

    @classmethod
    def _check_deletable(
        cls, connection: sqlite3.Connection, session_ids: set[str]
    ) -> None:
        marks = ",".join("?" for _ in session_ids)
        rows = connection.execute(
            f"SELECT session_id, archived_at, active_operation_id FROM conversation_sessions WHERE session_id IN ({marks})",
            list(session_ids),
        ).fetchall()
        if len(rows) != len(session_ids):
            missing = session_ids - {str(row["session_id"]) for row in rows}
            raise LookupError(f"ConversationSession 不存在: {sorted(missing)}")
        pending = connection.execute(
            f"SELECT 1 FROM agent_inbox_messages WHERE session_id IN ({marks}) AND status = 'pending' LIMIT 1",
            list(session_ids),
        ).fetchone()
        if pending is not None:
            raise StorageIntegrityError("删除要求无 pending InboxMessage")
        for row in rows:
            if row["archived_at"] is None:
                raise StorageIntegrityError("删除要求 Session 已归档")
            if row["active_operation_id"] is not None:
                raise StorageIntegrityError("删除要求 Session 空闲")


def _step_to_dict(step: Any) -> dict[str, Any] | None:
    if step is None:
        return None
    return step.content_dict()


def _json(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _object(value: Any) -> dict[str, Any]:
    try:
        result = json.loads(str(value))
    except (TypeError, json.JSONDecodeError) as exc:
        raise StorageIntegrityError("数据库 JSON 不是合法 object") from exc
    if not isinstance(result, dict):
        raise StorageIntegrityError("数据库 JSON 必须是 object")
    return result


def _time(value: Any) -> datetime:
    try:
        return datetime.fromisoformat(str(value))
    except ValueError as exc:
        raise StorageIntegrityError("数据库时间不是合法 ISO8601") from exc


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _optional(value: Any) -> str | None:
    return str(value) if value is not None else None


def _optional_time(value: Any) -> datetime | None:
    return _time(value) if value is not None else None


def _state_node_ids(state: AgentRunState) -> set[str]:
    """收集 State 对 ConversationNode 的全部强引用。"""
    result: set[str] = set()
    if state.final_assistant_node_id is not None:
        result.add(state.final_assistant_node_id)
    if state.current_step is not None:
        if state.current_step.assistant_message_node_id is not None:
            result.add(state.current_step.assistant_message_node_id)
        result.update(
            call.result_node_id
            for call in state.current_step.tool_calls
            if call.result_node_id is not None
        )
    return result
