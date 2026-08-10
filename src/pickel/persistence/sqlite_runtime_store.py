"""SQLite Runtime 存储：共享 commit_sequence 与原子事务。"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from pickel.artifacts.artifact import Artifact
from pickel.agents.agent_package import (
    AgentPackageVersion,
    agent_package_digest,
    agent_package_version_from_content,
)
from pickel.conversations.conversation_node import ConversationEntry, ConversationNode
from pickel.conversations.conversation_session import ConversationSession
from pickel.operations.agent_delegation import AgentDelegation
from pickel.operations.session_operation import SessionOperation
from pickel.persistence.immutable_object import (
    ImmutableObject,
    immutable_object_digest,
)
from pickel.persistence.named_reference import NamedReference
from pickel.persistence.storage_transaction import (
    StorageCommit,
    StorageConflictError,
    StorageIntegrityError,
    StorageTransaction,
)

SCHEMA_VERSION = 9


class UnsupportedStorageSchemaError(RuntimeError):
    pass


class SQLiteRuntimeStore:
    """Runtime 持久化窄接口的 SQLite 组合实现。"""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._schema_initialized = False

    @property
    def db_path(self) -> Path:
        return self._db_path

    def create_conversation_session(
        self,
        *,
        session_id: str,
        agent_id: str,
        cwd: str,
        created_at: datetime | None = None,
    ) -> None:
        self._ensure_schema()
        now = created_at or datetime.now(timezone.utc)
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO sessions (
                    session_id, agent_id, cwd, current_commit_sequence,
                    created_at, updated_at, status, title
                ) VALUES (?, ?, ?, 0, ?, ?, 'active', NULL)
                """,
                (session_id, agent_id, cwd, now.isoformat(), now.isoformat()),
            )

    def load_current_commit_sequence(self, session_id: str) -> int:
        self._ensure_schema()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT current_commit_sequence FROM sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        if row is None:
            raise LookupError(f"ConversationSession 不存在: {session_id}")
        return int(row["current_commit_sequence"])

    def insert_artifact(self, artifact: Artifact) -> None:
        self._ensure_schema()
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT * FROM artifacts WHERE artifact_id = ?",
                (artifact.artifact_id,),
            ).fetchone()
            if existing is not None:
                if self._artifact_from_row(existing) == artifact:
                    return
                raise StorageIntegrityError(
                    f"Artifact ID 已存在但内容不同: {artifact.artifact_id}"
                )
            connection.execute(
                """
                INSERT INTO artifacts (
                    artifact_id, digest, media_type, size_bytes,
                    blob_key, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    artifact.artifact_id,
                    artifact.digest,
                    artifact.media_type,
                    artifact.size_bytes,
                    artifact.blob_key,
                    artifact.created_at.isoformat(),
                ),
            )

    def load_artifact(self, artifact_id: str) -> Artifact | None:
        self._ensure_schema()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM artifacts WHERE artifact_id = ?",
                (artifact_id,),
            ).fetchone()
        return self._artifact_from_row(row) if row is not None else None

    def insert_agent_package_version(self, version: AgentPackageVersion) -> None:
        self._ensure_schema()
        content = version.content_dict()
        if agent_package_digest(content) != version.digest:
            raise StorageIntegrityError(
                f"AgentPackageVersion digest 校验失败: {version.package_version_id}"
            )
        with self._connect() as connection:
            existing = connection.execute(
                """
                SELECT digest, content_json
                FROM agent_package_versions
                WHERE package_version_id = ?
                """,
                (version.package_version_id,),
            ).fetchone()
            content_json = json.dumps(
                content,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            if existing is not None:
                if (
                    str(existing["digest"]) == version.digest
                    and str(existing["content_json"]) == content_json
                ):
                    return
                raise StorageIntegrityError(
                    "AgentPackageVersion ID 已存在但内容不同: "
                    f"{version.package_version_id}"
                )
            connection.execute(
                """
                INSERT INTO agent_package_versions (
                    package_version_id, digest, agent_id, content_json, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    version.package_version_id,
                    version.digest,
                    version.agent_id,
                    content_json,
                    version.created_at.isoformat(),
                ),
            )

    def load_agent_package_version(
        self,
        package_version_id: str,
    ) -> AgentPackageVersion | None:
        self._ensure_schema()
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT package_version_id, digest, content_json, created_at
                FROM agent_package_versions
                WHERE package_version_id = ?
                """,
                (package_version_id,),
            ).fetchone()
        if row is None:
            return None
        content = json.loads(str(row["content_json"]))
        if not isinstance(content, dict):
            raise StorageIntegrityError("AgentPackageVersion content 不是 JSON object")
        try:
            return agent_package_version_from_content(
                package_version_id=str(row["package_version_id"]),
                digest=str(row["digest"]),
                content=content,
                created_at=datetime.fromisoformat(str(row["created_at"])),
            )
        except (TypeError, ValueError, KeyError) as exc:
            raise StorageIntegrityError(str(exc)) from exc

    def load_session_operation(
        self,
        operation_id: str,
    ) -> SessionOperation | None:
        self._ensure_schema()
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM session_operations
                WHERE operation_id = ?
                """,
                (operation_id,),
            ).fetchone()
        return self._operation_from_row(row) if row is not None else None

    def list_session_operations(
        self,
        *,
        session_id: str,
    ) -> list[SessionOperation]:
        self._ensure_schema()
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM session_operations
                WHERE session_id = ?
                ORDER BY accepted_commit_sequence ASC, operation_id ASC
                """,
                (session_id,),
            ).fetchall()
        return [self._operation_from_row(row) for row in rows]

    def load_agent_delegation(
        self,
        delegation_id: str,
    ) -> AgentDelegation | None:
        self._ensure_schema()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM agent_delegations WHERE delegation_id = ?",
                (delegation_id,),
            ).fetchone()
        return self._delegation_from_row(row) if row is not None else None

    def find_delegation_by_child_operation(
        self,
        child_operation_id: str,
    ) -> AgentDelegation | None:
        self._ensure_schema()
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM agent_delegations
                WHERE child_operation_id = ?
                """,
                (child_operation_id,),
            ).fetchone()
        return self._delegation_from_row(row) if row is not None else None

    def list_agent_delegations(
        self,
        *,
        parent_operation_id: str,
    ) -> list[AgentDelegation]:
        self._ensure_schema()
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM agent_delegations
                WHERE parent_operation_id = ?
                ORDER BY created_at ASC, delegation_id ASC
                """,
                (parent_operation_id,),
            ).fetchall()
        return [self._delegation_from_row(row) for row in rows]

    def load_conversation_session(
        self,
        session_id: str,
    ) -> ConversationSession | None:
        self._ensure_schema()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if row is None:
                return None
            reference_row = self._find_reference_row(
                connection,
                session_id=session_id,
                reference_name="conversation/active",
            )
        return self._session_from_row(row, reference_row=reference_row)

    def list_conversation_sessions(
        self,
        *,
        limit: int = 20,
        cwd: str | None = None,
    ) -> list[ConversationSession]:
        if limit <= 0:
            return []
        self._ensure_schema()
        with self._connect() as connection:
            if cwd is None:
                rows = connection.execute(
                    """
                    SELECT * FROM sessions
                    ORDER BY updated_at DESC, session_id ASC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT * FROM sessions
                    WHERE cwd = ?
                    ORDER BY updated_at DESC, session_id ASC
                    LIMIT ?
                    """,
                    (cwd, limit),
                ).fetchall()
            sessions: list[ConversationSession] = []
            for row in rows:
                reference_row = self._find_reference_row(
                    connection,
                    session_id=str(row["session_id"]),
                    reference_name="conversation/active",
                )
                sessions.append(
                    self._session_from_row(row, reference_row=reference_row)
                )
        return sessions

    def archive_conversation_session(
        self,
        *,
        session_id: str,
        archived_at: datetime,
    ) -> None:
        self._ensure_schema()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE sessions
                SET status = 'archived', updated_at = ?
                WHERE session_id = ?
                """,
                (archived_at.isoformat(), session_id),
            )
            if cursor.rowcount != 1:
                raise LookupError(f"ConversationSession 不存在: {session_id}")

    def delete_conversation_session(self, *, session_id: str) -> None:
        self._ensure_schema()
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM sessions WHERE session_id = ?",
                (session_id,),
            )
            if cursor.rowcount != 1:
                raise LookupError(f"ConversationSession 不存在: {session_id}")

    def begin_storage_transaction(
        self,
        *,
        session_id: str,
        expected_commit_sequence: int,
    ) -> StorageTransaction:
        self._ensure_schema()
        return StorageTransaction(
            store=self,
            session_id=session_id,
            expected_commit_sequence=expected_commit_sequence,
        )

    def load_immutable_object(self, object_id: str) -> ImmutableObject | None:
        self._ensure_schema()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM immutable_objects WHERE object_id = ?",
                (object_id,),
            ).fetchone()
        return self._object_from_row(row) if row is not None else None

    def find_named_reference(
        self,
        *,
        session_id: str,
        reference_name: str,
    ) -> NamedReference | None:
        self._ensure_schema()
        with self._connect() as connection:
            row = self._find_reference_row(
                connection,
                session_id=session_id,
                reference_name=reference_name,
            )
        return self._reference_from_row(row) if row is not None else None

    def list_active_branch_entries(
        self,
        *,
        session_id: str,
        reference_name: str = "conversation/active",
    ) -> list[ConversationEntry]:
        self._ensure_schema()
        reference = self.find_named_reference(
            session_id=session_id,
            reference_name=reference_name,
        )
        if reference is None:
            return []
        if reference.target_kind != "node":
            raise StorageIntegrityError(
                f"活动会话引用必须指向 node: {reference.reference_name}"
            )
        with self._connect() as connection:
            rows = connection.execute(
                """
                WITH RECURSIVE active_branch(
                    node_id, session_id, parent_node_id, object_id,
                    created_commit_sequence, created_at, depth
                ) AS (
                    SELECT node_id, session_id, parent_node_id, object_id,
                           created_commit_sequence, created_at, 0
                    FROM conversation_nodes
                    WHERE node_id = ? AND session_id = ?
                    UNION ALL
                    SELECT parent.node_id, parent.session_id,
                           parent.parent_node_id, parent.object_id,
                           parent.created_commit_sequence, parent.created_at,
                           child.depth + 1
                    FROM conversation_nodes AS parent
                    JOIN active_branch AS child
                      ON parent.node_id = child.parent_node_id
                    WHERE parent.session_id = ?
                )
                SELECT
                    branch.node_id, branch.session_id, branch.parent_node_id,
                    branch.object_id, branch.created_commit_sequence,
                    branch.created_at,
                    object.object_type, object.schema_version, object.digest,
                    object.content_json, object.created_session_id,
                    object.created_commit_sequence,
                    object.created_at AS object_created_at
                FROM active_branch AS branch
                JOIN immutable_objects AS object
                  ON object.object_id = branch.object_id
                ORDER BY branch.depth DESC
                """,
                (reference.target_id, session_id, session_id),
            ).fetchall()
        if not rows:
            raise StorageIntegrityError(
                f"NamedReference 指向不存在的 ConversationNode: {reference.target_id}"
            )
        return [self._entry_from_row(row) for row in rows]

    def _commit_storage_transaction(
        self,
        transaction: StorageTransaction,
    ) -> StorageCommit:
        self._ensure_schema()
        committed_at = datetime.now(timezone.utc)
        commit_id = str(uuid4())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            session_row = connection.execute(
                "SELECT current_commit_sequence FROM sessions WHERE session_id = ?",
                (transaction.session_id,),
            ).fetchone()
            if session_row is None:
                raise LookupError(
                    f"ConversationSession 不存在: {transaction.session_id}"
                )
            current_commit_sequence = int(session_row["current_commit_sequence"])
            if current_commit_sequence != transaction.expected_commit_sequence:
                raise StorageConflictError(
                    "ConversationSession commit_sequence 冲突: "
                    f"expected={transaction.expected_commit_sequence}, "
                    f"actual={current_commit_sequence}"
                )
            commit_sequence = current_commit_sequence + 1
            self._validate_transaction(connection, transaction)
            connection.execute(
                """
                INSERT INTO storage_commits (
                    session_id, commit_sequence, commit_id, committed_at
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    transaction.session_id,
                    commit_sequence,
                    commit_id,
                    committed_at.isoformat(),
                ),
            )
            self._insert_objects(connection, transaction, commit_sequence, committed_at)
            self._insert_nodes(connection, transaction, commit_sequence, committed_at)
            self._insert_operations(
                connection,
                transaction,
                commit_sequence,
                committed_at,
            )
            self._insert_delegations(
                connection,
                transaction,
                commit_sequence,
                committed_at,
            )
            self._insert_references(connection, transaction, commit_sequence)
            connection.execute(
                """
                UPDATE sessions
                SET current_commit_sequence = ?, updated_at = ?
                WHERE session_id = ?
                """,
                (
                    commit_sequence,
                    committed_at.isoformat(),
                    transaction.session_id,
                ),
            )
        return StorageCommit(
            session_id=transaction.session_id,
            commit_sequence=commit_sequence,
            commit_id=commit_id,
            committed_at=committed_at,
        )

    def _validate_transaction(
        self,
        connection: sqlite3.Connection,
        transaction: StorageTransaction,
    ) -> None:
        if not (
            transaction.object_inserts
            or transaction.node_appends
            or transaction.reference_moves
            or transaction.operation_creates
            or transaction.delegation_creates
        ):
            raise StorageIntegrityError("StorageTransaction 不能为空")
        staged_object_ids = [
            command.object_id for command in transaction.object_inserts
        ]
        staged_node_ids = [command.node_id for command in transaction.node_appends]
        if len(staged_object_ids) != len(set(staged_object_ids)):
            raise StorageIntegrityError("同一事务包含重复 object_id")
        if len(staged_node_ids) != len(set(staged_node_ids)):
            raise StorageIntegrityError("同一事务包含重复 node_id")
        staged_operation_ids = [
            command.operation_id for command in transaction.operation_creates
        ]
        if len(staged_operation_ids) != len(set(staged_operation_ids)):
            raise StorageIntegrityError("同一事务包含重复 operation_id")
        delegation_ids = [
            command.delegation_id for command in transaction.delegation_creates
        ]
        if len(delegation_ids) != len(set(delegation_ids)):
            raise StorageIntegrityError("同一事务包含重复 delegation_id")
        child_operation_ids = [
            command.child_operation_id for command in transaction.delegation_creates
        ]
        if len(child_operation_ids) != len(set(child_operation_ids)):
            raise StorageIntegrityError("同一事务不能多次委派同一个 child_operation_id")
        for command in transaction.operation_creates:
            existing = connection.execute(
                "SELECT 1 FROM session_operations WHERE operation_id = ?",
                (command.operation_id,),
            ).fetchone()
            if existing is not None:
                raise StorageIntegrityError(
                    f"SessionOperation 已存在: {command.operation_id}"
                )
            package = connection.execute(
                """
                SELECT 1 FROM agent_package_versions
                WHERE package_version_id = ?
                """,
                (command.agent_package_version_id,),
            ).fetchone()
            if package is None:
                raise StorageIntegrityError(
                    "AgentPackageVersion 不存在: " f"{command.agent_package_version_id}"
                )

        for command in transaction.delegation_creates:
            parent = connection.execute(
                "SELECT 1 FROM session_operations WHERE operation_id = ?",
                (command.parent_operation_id,),
            ).fetchone()
            if parent is None:
                raise StorageIntegrityError(
                    f"父 SessionOperation 不存在: {command.parent_operation_id}"
                )
            if command.child_operation_id not in staged_operation_ids:
                raise StorageIntegrityError(
                    "child SessionOperation 必须与 AgentDelegation 同事务创建: "
                    f"{command.child_operation_id}"
                )
            existing = connection.execute(
                """
                SELECT 1 FROM agent_delegations
                WHERE delegation_id = ? OR child_operation_id = ?
                """,
                (command.delegation_id, command.child_operation_id),
            ).fetchone()
            if existing is not None:
                raise StorageIntegrityError(
                    "AgentDelegation 或 child SessionOperation 已存在: "
                    f"{command.delegation_id}"
                )

        for command in transaction.node_appends:
            if command.object_id not in staged_object_ids and not self._object_exists(
                connection,
                command.object_id,
            ):
                raise StorageIntegrityError(
                    f"ConversationNode 指向不存在的 Object: {command.object_id}"
                )
            if command.parent_node_id is not None:
                if command.parent_node_id in staged_node_ids:
                    parent = next(
                        item
                        for item in transaction.node_appends
                        if item.node_id == command.parent_node_id
                    )
                    if parent is command:
                        raise StorageIntegrityError("ConversationNode 不能指向自身")
                elif not self._node_belongs_to_session(
                    connection,
                    node_id=command.parent_node_id,
                    session_id=transaction.session_id,
                ):
                    raise StorageIntegrityError(
                        "parent_node_id 不存在或属于其他 Session: "
                        f"{command.parent_node_id}"
                    )

        moved_names: set[str] = set()
        for command in transaction.reference_moves:
            if command.reference_name in moved_names:
                raise StorageIntegrityError(
                    f"同一事务不能多次移动 Reference: {command.reference_name}"
                )
            moved_names.add(command.reference_name)
            current = self._find_reference_row(
                connection,
                session_id=transaction.session_id,
                reference_name=command.reference_name,
            )
            current_commit_sequence = (
                int(current["commit_sequence"]) if current is not None else None
            )
            if current_commit_sequence != command.expected_current_commit_sequence:
                raise StorageConflictError(
                    f"NamedReference commit_sequence 冲突: {command.reference_name}; "
                    f"expected={command.expected_current_commit_sequence}, "
                    f"actual={current_commit_sequence}"
                )
            if command.target_kind == "object":
                exists = command.target_id in staged_object_ids or self._object_exists(
                    connection,
                    command.target_id,
                )
            else:
                exists = (
                    command.target_id in staged_node_ids
                    or self._node_belongs_to_session(
                        connection,
                        node_id=command.target_id,
                        session_id=transaction.session_id,
                    )
                )
            if not exists:
                raise StorageIntegrityError(
                    f"NamedReference 指向不存在的 {command.target_kind}: "
                    f"{command.target_id}"
                )

    @staticmethod
    def _insert_objects(
        connection: sqlite3.Connection,
        transaction: StorageTransaction,
        commit_sequence: int,
        created_at: datetime,
    ) -> None:
        for command in transaction.object_inserts:
            digest = immutable_object_digest(
                object_type=command.object_type,
                schema_version=command.schema_version,
                content=command.content,
            )
            try:
                connection.execute(
                    """
                    INSERT INTO immutable_objects (
                        object_id, object_type, schema_version, digest,
                        content_json, created_session_id,
                        created_commit_sequence, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        command.object_id,
                        command.object_type,
                        command.schema_version,
                        digest,
                        json.dumps(command.content, ensure_ascii=False),
                        transaction.session_id,
                        commit_sequence,
                        created_at.isoformat(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise StorageIntegrityError(
                    f"ImmutableObject 写入失败: {command.object_id}"
                ) from exc

    @staticmethod
    def _insert_nodes(
        connection: sqlite3.Connection,
        transaction: StorageTransaction,
        commit_sequence: int,
        created_at: datetime,
    ) -> None:
        pending = list(transaction.node_appends)
        inserted: set[str] = set()
        while pending:
            progressed = False
            for command in list(pending):
                if (
                    command.parent_node_id is not None
                    and command.parent_node_id
                    in {item.node_id for item in transaction.node_appends}
                    and command.parent_node_id not in inserted
                ):
                    continue
                try:
                    connection.execute(
                        """
                        INSERT INTO conversation_nodes (
                            node_id, session_id, parent_node_id,
                            object_id, created_commit_sequence, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            command.node_id,
                            transaction.session_id,
                            command.parent_node_id,
                            command.object_id,
                            commit_sequence,
                            created_at.isoformat(),
                        ),
                    )
                except sqlite3.IntegrityError as exc:
                    raise StorageIntegrityError(
                        f"ConversationNode 写入失败: {command.node_id}"
                    ) from exc
                pending.remove(command)
                inserted.add(command.node_id)
                progressed = True
            if not progressed:
                raise StorageIntegrityError(
                    "同一事务中的 ConversationNode 存在 parent 环"
                )

    @staticmethod
    def _insert_references(
        connection: sqlite3.Connection,
        transaction: StorageTransaction,
        commit_sequence: int,
    ) -> None:
        for command in transaction.reference_moves:
            connection.execute(
                """
                INSERT INTO named_references (
                    session_id, reference_name, commit_sequence,
                    target_kind, target_id
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    transaction.session_id,
                    command.reference_name,
                    commit_sequence,
                    command.target_kind,
                    command.target_id,
                ),
            )

    @staticmethod
    def _insert_operations(
        connection: sqlite3.Connection,
        transaction: StorageTransaction,
        commit_sequence: int,
        created_at: datetime,
    ) -> None:
        for command in transaction.operation_creates:
            connection.execute(
                """
                INSERT INTO session_operations (
                    operation_id, session_id, operation_type,
                    agent_package_version_id, accepted_commit_sequence,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    command.operation_id,
                    transaction.session_id,
                    command.operation_type,
                    command.agent_package_version_id,
                    commit_sequence,
                    created_at.isoformat(),
                ),
            )

    @staticmethod
    def _insert_delegations(
        connection: sqlite3.Connection,
        transaction: StorageTransaction,
        commit_sequence: int,
        created_at: datetime,
    ) -> None:
        for command in transaction.delegation_creates:
            connection.execute(
                """
                INSERT INTO agent_delegations (
                    delegation_id, parent_operation_id, parent_step_id,
                    parent_tool_call_id, child_operation_id, child_session_id,
                    created_commit_sequence, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    command.delegation_id,
                    command.parent_operation_id,
                    command.parent_step_id,
                    command.parent_tool_call_id,
                    command.child_operation_id,
                    transaction.session_id,
                    commit_sequence,
                    created_at.isoformat(),
                ),
            )

    @staticmethod
    def _find_reference_row(
        connection: sqlite3.Connection,
        *,
        session_id: str,
        reference_name: str,
    ) -> sqlite3.Row | None:
        return connection.execute(
            """
            SELECT session_id, reference_name, commit_sequence,
                   target_kind, target_id
            FROM named_references
            WHERE session_id = ? AND reference_name = ?
            ORDER BY commit_sequence DESC
            LIMIT 1
            """,
            (session_id, reference_name),
        ).fetchone()

    @staticmethod
    def _object_exists(connection: sqlite3.Connection, object_id: str) -> bool:
        return (
            connection.execute(
                "SELECT 1 FROM immutable_objects WHERE object_id = ?",
                (object_id,),
            ).fetchone()
            is not None
        )

    @staticmethod
    def _node_belongs_to_session(
        connection: sqlite3.Connection,
        *,
        node_id: str,
        session_id: str,
    ) -> bool:
        return (
            connection.execute(
                """
                SELECT 1 FROM conversation_nodes
                WHERE node_id = ? AND session_id = ?
                """,
                (node_id, session_id),
            ).fetchone()
            is not None
        )

    def _ensure_schema(self) -> None:
        if self._schema_initialized and self._db_path.exists():
            return
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version not in {0, SCHEMA_VERSION}:
                raise UnsupportedStorageSchemaError(
                    f"不支持的 SQLite schema version: {version}; "
                    f"需要 {SCHEMA_VERSION}"
                )
            if version == 0:
                connection.executescript(self._schema_sql())
        self._schema_initialized = True

    @staticmethod
    def _schema_sql() -> str:
        return """
            PRAGMA user_version = 9;

            CREATE TABLE sessions (
                session_id TEXT PRIMARY KEY,
                agent_id TEXT NOT NULL,
                cwd TEXT NOT NULL,
                current_commit_sequence INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                status TEXT NOT NULL,
                title TEXT
            );

            CREATE TABLE agent_package_versions (
                package_version_id TEXT PRIMARY KEY,
                digest TEXT NOT NULL UNIQUE,
                agent_id TEXT NOT NULL,
                content_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE INDEX idx_agent_package_versions_agent
            ON agent_package_versions(agent_id, created_at DESC);

            CREATE TABLE artifacts (
                artifact_id TEXT PRIMARY KEY,
                digest TEXT NOT NULL,
                media_type TEXT NOT NULL,
                size_bytes INTEGER NOT NULL CHECK(size_bytes >= 0),
                blob_key TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE INDEX idx_artifacts_digest ON artifacts(digest);

            CREATE TABLE session_operations (
                operation_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                operation_type TEXT NOT NULL CHECK(operation_type IN ('agent_run')),
                agent_package_version_id TEXT NOT NULL,
                accepted_commit_sequence INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (session_id, accepted_commit_sequence)
                    REFERENCES storage_commits(session_id, commit_sequence)
                    ON DELETE CASCADE,
                FOREIGN KEY (agent_package_version_id)
                    REFERENCES agent_package_versions(package_version_id)
            );

            CREATE INDEX idx_session_operations_session_commit_sequence
            ON session_operations(session_id, accepted_commit_sequence);

            CREATE TABLE agent_delegations (
                delegation_id TEXT PRIMARY KEY,
                parent_operation_id TEXT NOT NULL,
                parent_step_id TEXT NOT NULL,
                parent_tool_call_id TEXT,
                child_operation_id TEXT NOT NULL UNIQUE,
                child_session_id TEXT NOT NULL,
                created_commit_sequence INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (parent_operation_id)
                    REFERENCES session_operations(operation_id),
                FOREIGN KEY (child_operation_id)
                    REFERENCES session_operations(operation_id)
                    ON DELETE CASCADE,
                FOREIGN KEY (child_session_id, created_commit_sequence)
                    REFERENCES storage_commits(session_id, commit_sequence)
                    ON DELETE CASCADE
            );

            CREATE INDEX idx_agent_delegations_parent
            ON agent_delegations(parent_operation_id, created_at);

            CREATE TABLE storage_commits (
                session_id TEXT NOT NULL,
                commit_sequence INTEGER NOT NULL,
                commit_id TEXT NOT NULL UNIQUE,
                committed_at TEXT NOT NULL,
                PRIMARY KEY (session_id, commit_sequence),
                FOREIGN KEY (session_id) REFERENCES sessions(session_id)
                    ON DELETE CASCADE
            );

            CREATE TABLE immutable_objects (
                object_id TEXT PRIMARY KEY,
                object_type TEXT NOT NULL,
                schema_version INTEGER NOT NULL,
                digest TEXT NOT NULL,
                content_json TEXT NOT NULL,
                created_session_id TEXT NOT NULL,
                created_commit_sequence INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (created_session_id, created_commit_sequence)
                    REFERENCES storage_commits(session_id, commit_sequence)
                    ON DELETE CASCADE
            );

            CREATE INDEX idx_immutable_objects_digest
            ON immutable_objects(digest);

            CREATE TABLE conversation_nodes (
                node_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                parent_node_id TEXT,
                object_id TEXT NOT NULL,
                created_commit_sequence INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (session_id, created_commit_sequence)
                    REFERENCES storage_commits(session_id, commit_sequence)
                    ON DELETE CASCADE,
                FOREIGN KEY (parent_node_id) REFERENCES conversation_nodes(node_id),
                FOREIGN KEY (object_id) REFERENCES immutable_objects(object_id)
            );

            CREATE INDEX idx_conversation_nodes_session_parent
            ON conversation_nodes(session_id, parent_node_id);

            CREATE TABLE named_references (
                session_id TEXT NOT NULL,
                reference_name TEXT NOT NULL,
                commit_sequence INTEGER NOT NULL,
                target_kind TEXT NOT NULL CHECK(target_kind IN ('node', 'object')),
                target_id TEXT NOT NULL,
                PRIMARY KEY (session_id, reference_name, commit_sequence),
                FOREIGN KEY (session_id, commit_sequence)
                    REFERENCES storage_commits(session_id, commit_sequence)
                    ON DELETE CASCADE
            );

            CREATE INDEX idx_named_references_current
            ON named_references(
                session_id, reference_name, commit_sequence DESC
            );

            CREATE INDEX idx_sessions_agent_updated
            ON sessions(agent_id, updated_at DESC);

            CREATE INDEX idx_sessions_cwd_updated
            ON sessions(cwd, updated_at DESC);
        """

    def _connect(self) -> sqlite3.Connection:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self._db_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @staticmethod
    def _session_from_row(
        row: sqlite3.Row,
        *,
        reference_row: sqlite3.Row | None,
    ) -> ConversationSession:
        active_node_id = None
        if reference_row is not None:
            if str(reference_row["target_kind"]) != "node":
                raise StorageIntegrityError("conversation/active 必须指向 node")
            active_node_id = str(reference_row["target_id"])
        return ConversationSession(
            session_id=str(row["session_id"]),
            agent_id=str(row["agent_id"]),
            cwd=str(row["cwd"]),
            current_commit_sequence=int(row["current_commit_sequence"]),
            active_node_id=active_node_id,
            created_at=datetime.fromisoformat(str(row["created_at"])),
            updated_at=datetime.fromisoformat(str(row["updated_at"])),
            status=str(row["status"]),
            title=str(row["title"]) if row["title"] is not None else None,
        )

    @staticmethod
    def _artifact_from_row(row: sqlite3.Row) -> Artifact:
        return Artifact(
            artifact_id=str(row["artifact_id"]),
            digest=str(row["digest"]),
            media_type=str(row["media_type"]),
            size_bytes=int(row["size_bytes"]),
            blob_key=str(row["blob_key"]),
            created_at=datetime.fromisoformat(str(row["created_at"])),
        )

    @staticmethod
    def _object_from_row(row: sqlite3.Row) -> ImmutableObject:
        content = json.loads(str(row["content_json"]))
        if not isinstance(content, dict):
            raise StorageIntegrityError("ImmutableObject content_json 不是 JSON object")
        expected_digest = immutable_object_digest(
            object_type=str(row["object_type"]),
            schema_version=int(row["schema_version"]),
            content=content,
        )
        if expected_digest != str(row["digest"]):
            raise StorageIntegrityError(
                f"ImmutableObject digest 校验失败: {row['object_id']}"
            )
        return ImmutableObject(
            object_id=str(row["object_id"]),
            object_type=str(row["object_type"]),
            schema_version=int(row["schema_version"]),
            digest=str(row["digest"]),
            content=content,
            created_session_id=str(row["created_session_id"]),
            created_commit_sequence=int(row["created_commit_sequence"]),
            created_at=datetime.fromisoformat(str(row["created_at"])),
        )

    @staticmethod
    def _operation_from_row(row: sqlite3.Row) -> SessionOperation:
        return SessionOperation(
            operation_id=str(row["operation_id"]),
            session_id=str(row["session_id"]),
            operation_type=str(row["operation_type"]),  # type: ignore[arg-type]
            agent_package_version_id=str(row["agent_package_version_id"]),
            accepted_commit_sequence=int(row["accepted_commit_sequence"]),
            created_at=datetime.fromisoformat(str(row["created_at"])),
        )

    @staticmethod
    def _delegation_from_row(row: sqlite3.Row) -> AgentDelegation:
        return AgentDelegation(
            delegation_id=str(row["delegation_id"]),
            parent_operation_id=str(row["parent_operation_id"]),
            parent_step_id=str(row["parent_step_id"]),
            parent_tool_call_id=(
                str(row["parent_tool_call_id"])
                if row["parent_tool_call_id"] is not None
                else None
            ),
            child_operation_id=str(row["child_operation_id"]),
            child_session_id=str(row["child_session_id"]),
            created_commit_sequence=int(row["created_commit_sequence"]),
            created_at=datetime.fromisoformat(str(row["created_at"])),
        )

    @staticmethod
    def _reference_from_row(row: sqlite3.Row) -> NamedReference:
        return NamedReference(
            session_id=str(row["session_id"]),
            reference_name=str(row["reference_name"]),
            commit_sequence=int(row["commit_sequence"]),
            target_kind=str(row["target_kind"]),  # type: ignore[arg-type]
            target_id=str(row["target_id"]),
        )

    @classmethod
    def _entry_from_row(cls, row: sqlite3.Row) -> ConversationEntry:
        node = ConversationNode(
            node_id=str(row["node_id"]),
            session_id=str(row["session_id"]),
            parent_node_id=(
                str(row["parent_node_id"])
                if row["parent_node_id"] is not None
                else None
            ),
            object_id=str(row["object_id"]),
            created_commit_sequence=int(row["created_commit_sequence"]),
            created_at=datetime.fromisoformat(str(row["created_at"])),
        )
        object_row: dict[str, Any] = {
            "object_id": row["object_id"],
            "object_type": row["object_type"],
            "schema_version": row["schema_version"],
            "digest": row["digest"],
            "content_json": row["content_json"],
            "created_session_id": row["created_session_id"],
            "created_commit_sequence": row["created_commit_sequence"],
            "created_at": row["object_created_at"],
        }
        immutable_object = cls._object_from_row(object_row)  # type: ignore[arg-type]
        return ConversationEntry(node=node, object=immutable_object)
