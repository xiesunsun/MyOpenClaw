"""SQLite v4 会话存储：共享 sequence 与原子 StorageTransaction。"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from pickel.persistence.conversation_node import ConversationEntry, ConversationNode
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

SCHEMA_VERSION = 4


class UnsupportedStorageSchemaError(RuntimeError):
    pass


class SQLiteConversationStore:
    """Conversation 持久化事实的 SQLite 实现。"""

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
                    session_id, agent_id, cwd, current_sequence,
                    created_at, updated_at, status, title
                ) VALUES (?, ?, ?, 0, ?, ?, 'active', NULL)
                """,
                (session_id, agent_id, cwd, now.isoformat(), now.isoformat()),
            )

    def load_current_sequence(self, session_id: str) -> int:
        self._ensure_schema()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT current_sequence FROM sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        if row is None:
            raise LookupError(f"ConversationSession 不存在: {session_id}")
        return int(row["current_sequence"])

    def begin_storage_transaction(
        self,
        *,
        session_id: str,
        expected_sequence: int,
    ) -> StorageTransaction:
        self._ensure_schema()
        return StorageTransaction(
            store=self,
            session_id=session_id,
            expected_sequence=expected_sequence,
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
                    sequence, created_at, depth
                ) AS (
                    SELECT node_id, session_id, parent_node_id, object_id,
                           sequence, created_at, 0
                    FROM conversation_nodes
                    WHERE node_id = ? AND session_id = ?
                    UNION ALL
                    SELECT parent.node_id, parent.session_id,
                           parent.parent_node_id, parent.object_id,
                           parent.sequence, parent.created_at, child.depth + 1
                    FROM conversation_nodes AS parent
                    JOIN active_branch AS child
                      ON parent.node_id = child.parent_node_id
                    WHERE parent.session_id = ?
                )
                SELECT
                    branch.node_id, branch.session_id, branch.parent_node_id,
                    branch.object_id, branch.sequence, branch.created_at,
                    object.object_type, object.schema_version, object.digest,
                    object.content_json, object.created_session_id,
                    object.created_sequence, object.created_at AS object_created_at
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
                "SELECT current_sequence FROM sessions WHERE session_id = ?",
                (transaction.session_id,),
            ).fetchone()
            if session_row is None:
                raise LookupError(
                    f"ConversationSession 不存在: {transaction.session_id}"
                )
            current_sequence = int(session_row["current_sequence"])
            if current_sequence != transaction.expected_sequence:
                raise StorageConflictError(
                    "ConversationSession sequence 冲突: "
                    f"expected={transaction.expected_sequence}, "
                    f"actual={current_sequence}"
                )
            sequence = current_sequence + 1
            self._validate_transaction(connection, transaction)
            connection.execute(
                """
                INSERT INTO storage_commits (
                    session_id, sequence, commit_id, committed_at
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    transaction.session_id,
                    sequence,
                    commit_id,
                    committed_at.isoformat(),
                ),
            )
            self._insert_objects(connection, transaction, sequence, committed_at)
            self._insert_nodes(connection, transaction, sequence, committed_at)
            self._insert_references(connection, transaction, sequence)
            connection.execute(
                """
                UPDATE sessions
                SET current_sequence = ?, updated_at = ?
                WHERE session_id = ?
                """,
                (sequence, committed_at.isoformat(), transaction.session_id),
            )
        return StorageCommit(
            session_id=transaction.session_id,
            sequence=sequence,
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
            current_sequence = int(current["sequence"]) if current is not None else None
            if current_sequence != command.expected_current_sequence:
                raise StorageConflictError(
                    f"NamedReference sequence 冲突: {command.reference_name}; "
                    f"expected={command.expected_current_sequence}, "
                    f"actual={current_sequence}"
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
        sequence: int,
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
                        content_json, created_session_id, created_sequence, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        command.object_id,
                        command.object_type,
                        command.schema_version,
                        digest,
                        json.dumps(command.content, ensure_ascii=False),
                        transaction.session_id,
                        sequence,
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
        sequence: int,
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
                            object_id, sequence, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            command.node_id,
                            transaction.session_id,
                            command.parent_node_id,
                            command.object_id,
                            sequence,
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
        sequence: int,
    ) -> None:
        for command in transaction.reference_moves:
            connection.execute(
                """
                INSERT INTO named_references (
                    session_id, reference_name, sequence, target_kind, target_id
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    transaction.session_id,
                    command.reference_name,
                    sequence,
                    command.target_kind,
                    command.target_id,
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
            SELECT session_id, reference_name, sequence, target_kind, target_id
            FROM named_references
            WHERE session_id = ? AND reference_name = ?
            ORDER BY sequence DESC
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
            PRAGMA user_version = 4;

            CREATE TABLE sessions (
                session_id TEXT PRIMARY KEY,
                agent_id TEXT NOT NULL,
                cwd TEXT NOT NULL,
                current_sequence INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                status TEXT NOT NULL,
                title TEXT
            );

            CREATE TABLE storage_commits (
                session_id TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                commit_id TEXT NOT NULL UNIQUE,
                committed_at TEXT NOT NULL,
                PRIMARY KEY (session_id, sequence),
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
                created_sequence INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (created_session_id, created_sequence)
                    REFERENCES storage_commits(session_id, sequence)
                    ON DELETE CASCADE
            );

            CREATE INDEX idx_immutable_objects_digest
            ON immutable_objects(digest);

            CREATE TABLE conversation_nodes (
                node_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                parent_node_id TEXT,
                object_id TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (session_id, sequence)
                    REFERENCES storage_commits(session_id, sequence)
                    ON DELETE CASCADE,
                FOREIGN KEY (parent_node_id) REFERENCES conversation_nodes(node_id),
                FOREIGN KEY (object_id) REFERENCES immutable_objects(object_id)
            );

            CREATE INDEX idx_conversation_nodes_session_parent
            ON conversation_nodes(session_id, parent_node_id);

            CREATE TABLE named_references (
                session_id TEXT NOT NULL,
                reference_name TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                target_kind TEXT NOT NULL CHECK(target_kind IN ('node', 'object')),
                target_id TEXT NOT NULL,
                PRIMARY KEY (session_id, reference_name, sequence),
                FOREIGN KEY (session_id, sequence)
                    REFERENCES storage_commits(session_id, sequence)
                    ON DELETE CASCADE
            );

            CREATE INDEX idx_named_references_current
            ON named_references(session_id, reference_name, sequence DESC);

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
            created_sequence=int(row["created_sequence"]),
            created_at=datetime.fromisoformat(str(row["created_at"])),
        )

    @staticmethod
    def _reference_from_row(row: sqlite3.Row) -> NamedReference:
        return NamedReference(
            session_id=str(row["session_id"]),
            reference_name=str(row["reference_name"]),
            sequence=int(row["sequence"]),
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
            sequence=int(row["sequence"]),
            created_at=datetime.fromisoformat(str(row["created_at"])),
        )
        object_row: dict[str, Any] = {
            "object_id": row["object_id"],
            "object_type": row["object_type"],
            "schema_version": row["schema_version"],
            "digest": row["digest"],
            "content_json": row["content_json"],
            "created_session_id": row["created_session_id"],
            "created_sequence": row["created_sequence"],
            "created_at": row["object_created_at"],
        }
        immutable_object = cls._object_from_row(object_row)  # type: ignore[arg-type]
        return ConversationEntry(node=node, object=immutable_object)
