"""进程内 ConversationStore；与 SQLite 适配器遵循相同事务合同。"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone
from threading import RLock
from uuid import uuid4

from pickel.agents.agent_package import (
    AgentPackageVersion,
    agent_package_digest,
    agent_package_version_from_content,
)
from pickel.conversations.conversation_node import ConversationEntry, ConversationNode
from pickel.conversations.conversation_session import ConversationSession
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


class InMemoryRuntimeStore:
    """用于非持久 Runtime；与 SQLite 实现遵循相同领域合同。"""

    def __init__(self) -> None:
        self._sessions: dict[str, ConversationSession] = {}
        self._agent_package_versions: dict[str, tuple[str, dict, datetime]] = {}
        self._operations: dict[str, SessionOperation] = {}
        self._commits: dict[tuple[str, int], StorageCommit] = {}
        self._objects: dict[str, ImmutableObject] = {}
        self._nodes: dict[str, ConversationNode] = {}
        self._references: dict[tuple[str, str], list[NamedReference]] = {}
        self._lock = RLock()

    def create_conversation_session(
        self,
        *,
        session_id: str,
        agent_id: str,
        cwd: str,
        created_at: datetime | None = None,
    ) -> None:
        now = created_at or datetime.now(timezone.utc)
        with self._lock:
            if session_id in self._sessions:
                raise StorageIntegrityError(f"ConversationSession 已存在: {session_id}")
            self._sessions[session_id] = ConversationSession(
                session_id=session_id,
                agent_id=agent_id,
                cwd=cwd,
                current_commit_sequence=0,
                active_node_id=None,
                created_at=now,
                updated_at=now,
            )

    def load_current_commit_sequence(self, session_id: str) -> int:
        session = self.load_conversation_session(session_id)
        if session is None:
            raise LookupError(f"ConversationSession 不存在: {session_id}")
        return session.current_commit_sequence

    def load_conversation_session(
        self,
        session_id: str,
    ) -> ConversationSession | None:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return None
            reference = self._find_named_reference_unlocked(
                session_id=session_id,
                reference_name="conversation/active",
            )
            active_node_id = None
            if reference is not None:
                if reference.target_kind != "node":
                    raise StorageIntegrityError("conversation/active 必须指向 node")
                active_node_id = reference.target_id
            return replace(session, active_node_id=active_node_id)

    def list_conversation_sessions(
        self,
        *,
        limit: int = 20,
        cwd: str | None = None,
    ) -> list[ConversationSession]:
        if limit <= 0:
            return []
        with self._lock:
            session_ids = [
                session.session_id
                for session in sorted(
                    self._sessions.values(),
                    key=lambda item: (-item.updated_at.timestamp(), item.session_id),
                )
                if cwd is None or session.cwd == cwd
            ][:limit]
        return [
            session
            for session_id in session_ids
            if (session := self.load_conversation_session(session_id)) is not None
        ]

    def archive_conversation_session(
        self,
        *,
        session_id: str,
        archived_at: datetime,
    ) -> None:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                raise LookupError(f"ConversationSession 不存在: {session_id}")
            self._sessions[session_id] = replace(
                session,
                status="archived",
                updated_at=archived_at,
            )

    def delete_conversation_session(self, *, session_id: str) -> None:
        with self._lock:
            if session_id not in self._sessions:
                raise LookupError(f"ConversationSession 不存在: {session_id}")
            del self._sessions[session_id]
            self._commits = {
                key: value
                for key, value in self._commits.items()
                if key[0] != session_id
            }
            object_ids = {
                object_id
                for object_id, value in self._objects.items()
                if value.created_session_id == session_id
            }
            self._objects = {
                key: value
                for key, value in self._objects.items()
                if key not in object_ids
            }
            self._nodes = {
                key: value
                for key, value in self._nodes.items()
                if value.session_id != session_id
            }
            self._references = {
                key: value
                for key, value in self._references.items()
                if key[0] != session_id
            }
            self._operations = {
                key: value
                for key, value in self._operations.items()
                if value.session_id != session_id
            }

    def insert_agent_package_version(self, version: AgentPackageVersion) -> None:
        content = version.content_dict()
        if agent_package_digest(content) != version.digest:
            raise StorageIntegrityError(
                f"AgentPackageVersion digest 校验失败: {version.package_version_id}"
            )
        copied = self._copy_content(content)
        with self._lock:
            existing = self._agent_package_versions.get(version.package_version_id)
            if existing is not None:
                if existing[0] == version.digest and existing[1] == copied:
                    return
                raise StorageIntegrityError(
                    "AgentPackageVersion ID 已存在但内容不同: "
                    f"{version.package_version_id}"
                )
            self._agent_package_versions[version.package_version_id] = (
                version.digest,
                copied,
                version.created_at,
            )

    def load_agent_package_version(
        self,
        package_version_id: str,
    ) -> AgentPackageVersion | None:
        with self._lock:
            stored = self._agent_package_versions.get(package_version_id)
            if stored is None:
                return None
            digest, content, created_at = stored
            return agent_package_version_from_content(
                package_version_id=package_version_id,
                digest=digest,
                content=self._copy_content(content),
                created_at=created_at,
            )

    def load_session_operation(
        self,
        operation_id: str,
    ) -> SessionOperation | None:
        with self._lock:
            return self._operations.get(operation_id)

    def list_session_operations(
        self,
        *,
        session_id: str,
    ) -> list[SessionOperation]:
        with self._lock:
            return sorted(
                (
                    operation
                    for operation in self._operations.values()
                    if operation.session_id == session_id
                ),
                key=lambda item: (item.accepted_commit_sequence, item.operation_id),
            )

    def begin_storage_transaction(
        self,
        *,
        session_id: str,
        expected_commit_sequence: int,
    ) -> StorageTransaction:
        return StorageTransaction(
            store=self,
            session_id=session_id,
            expected_commit_sequence=expected_commit_sequence,
        )

    def load_immutable_object(self, object_id: str) -> ImmutableObject | None:
        with self._lock:
            value = self._objects.get(object_id)
            return self._copy_object(value) if value is not None else None

    def find_named_reference(
        self,
        *,
        session_id: str,
        reference_name: str,
    ) -> NamedReference | None:
        with self._lock:
            return self._find_named_reference_unlocked(
                session_id=session_id,
                reference_name=reference_name,
            )

    def list_active_branch_entries(
        self,
        *,
        session_id: str,
        reference_name: str = "conversation/active",
    ) -> list[ConversationEntry]:
        with self._lock:
            if session_id not in self._sessions:
                raise LookupError(f"ConversationSession 不存在: {session_id}")
            reference = self._find_named_reference_unlocked(
                session_id=session_id,
                reference_name=reference_name,
            )
            if reference is None:
                return []
            if reference.target_kind != "node":
                raise StorageIntegrityError(
                    f"活动会话引用必须指向 node: {reference.reference_name}"
                )

            reversed_entries: list[ConversationEntry] = []
            visited: set[str] = set()
            node_id: str | None = reference.target_id
            while node_id is not None:
                if node_id in visited:
                    raise StorageIntegrityError("ConversationNode parent 链存在环")
                visited.add(node_id)
                node = self._nodes.get(node_id)
                if node is None or node.session_id != session_id:
                    raise StorageIntegrityError(
                        f"NamedReference 指向不存在的 ConversationNode: {node_id}"
                    )
                immutable_object = self._objects.get(node.object_id)
                if immutable_object is None:
                    raise StorageIntegrityError(
                        f"ConversationNode 指向不存在的 Object: {node.object_id}"
                    )
                reversed_entries.append(
                    ConversationEntry(
                        node=node,
                        object=self._copy_object(immutable_object),
                    )
                )
                node_id = node.parent_node_id
            return list(reversed(reversed_entries))

    def _commit_storage_transaction(
        self,
        transaction: StorageTransaction,
    ) -> StorageCommit:
        committed_at = datetime.now(timezone.utc)
        with self._lock:
            session = self._sessions.get(transaction.session_id)
            if session is None:
                raise LookupError(
                    f"ConversationSession 不存在: {transaction.session_id}"
                )
            if session.current_commit_sequence != transaction.expected_commit_sequence:
                raise StorageConflictError(
                    "ConversationSession commit_sequence 冲突: "
                    f"expected={transaction.expected_commit_sequence}, "
                    f"actual={session.current_commit_sequence}"
                )
            self._validate_transaction(transaction)
            commit_sequence = session.current_commit_sequence + 1
            commit = StorageCommit(
                session_id=transaction.session_id,
                commit_sequence=commit_sequence,
                commit_id=str(uuid4()),
                committed_at=committed_at,
            )

            staged_objects = {
                command.object_id: ImmutableObject(
                    object_id=command.object_id,
                    object_type=command.object_type,
                    schema_version=command.schema_version,
                    digest=immutable_object_digest(
                        object_type=command.object_type,
                        schema_version=command.schema_version,
                        content=command.content,
                    ),
                    content=self._copy_content(command.content),
                    created_session_id=transaction.session_id,
                    created_commit_sequence=commit_sequence,
                    created_at=committed_at,
                )
                for command in transaction.object_inserts
            }
            staged_nodes = {
                command.node_id: ConversationNode(
                    node_id=command.node_id,
                    session_id=transaction.session_id,
                    parent_node_id=command.parent_node_id,
                    object_id=command.object_id,
                    created_commit_sequence=commit_sequence,
                    created_at=committed_at,
                )
                for command in transaction.node_appends
            }
            staged_references = [
                NamedReference(
                    session_id=transaction.session_id,
                    reference_name=command.reference_name,
                    commit_sequence=commit_sequence,
                    target_kind=command.target_kind,
                    target_id=command.target_id,
                )
                for command in transaction.reference_moves
            ]
            staged_operations = {
                command.operation_id: SessionOperation(
                    operation_id=command.operation_id,
                    session_id=transaction.session_id,
                    operation_type=command.operation_type,  # type: ignore[arg-type]
                    agent_package_version_id=command.agent_package_version_id,
                    accepted_commit_sequence=commit_sequence,
                    created_at=committed_at,
                )
                for command in transaction.operation_creates
            }

            self._commits[(transaction.session_id, commit_sequence)] = commit
            self._objects.update(staged_objects)
            self._nodes.update(staged_nodes)
            self._operations.update(staged_operations)
            for reference in staged_references:
                self._references.setdefault(
                    (reference.session_id, reference.reference_name), []
                ).append(reference)
            self._sessions[transaction.session_id] = replace(
                session,
                current_commit_sequence=commit_sequence,
                updated_at=committed_at,
            )
            return commit

    def _validate_transaction(self, transaction: StorageTransaction) -> None:
        if not (
            transaction.object_inserts
            or transaction.node_appends
            or transaction.reference_moves
            or transaction.operation_creates
        ):
            raise StorageIntegrityError("StorageTransaction 不能为空")

        object_ids = [command.object_id for command in transaction.object_inserts]
        node_ids = [command.node_id for command in transaction.node_appends]
        if len(object_ids) != len(set(object_ids)):
            raise StorageIntegrityError("同一事务包含重复 object_id")
        if len(node_ids) != len(set(node_ids)):
            raise StorageIntegrityError("同一事务包含重复 node_id")
        operation_ids = [
            command.operation_id for command in transaction.operation_creates
        ]
        if len(operation_ids) != len(set(operation_ids)):
            raise StorageIntegrityError("同一事务包含重复 operation_id")
        duplicate_object = next(
            (object_id for object_id in object_ids if object_id in self._objects),
            None,
        )
        if duplicate_object is not None:
            raise StorageIntegrityError(f"ImmutableObject 写入失败: {duplicate_object}")
        duplicate_node = next(
            (node_id for node_id in node_ids if node_id in self._nodes),
            None,
        )
        if duplicate_node is not None:
            raise StorageIntegrityError(f"ConversationNode 写入失败: {duplicate_node}")
        for command in transaction.operation_creates:
            if command.operation_id in self._operations:
                raise StorageIntegrityError(
                    f"SessionOperation 已存在: {command.operation_id}"
                )
            if command.agent_package_version_id not in self._agent_package_versions:
                raise StorageIntegrityError(
                    "AgentPackageVersion 不存在: " f"{command.agent_package_version_id}"
                )

        staged_object_ids = set(object_ids)
        staged_node_ids = set(node_ids)
        parent_by_node = {
            command.node_id: command.parent_node_id
            for command in transaction.node_appends
        }
        for command in transaction.node_appends:
            if (
                command.object_id not in staged_object_ids
                and command.object_id not in self._objects
            ):
                raise StorageIntegrityError(
                    f"ConversationNode 指向不存在的 Object: {command.object_id}"
                )
            parent_id = command.parent_node_id
            if parent_id is None:
                continue
            if parent_id in staged_node_ids:
                self._ensure_acyclic_parent_chain(
                    node_id=command.node_id,
                    parent_by_node=parent_by_node,
                )
                continue
            parent = self._nodes.get(parent_id)
            if parent is None or parent.session_id != transaction.session_id:
                raise StorageIntegrityError(
                    "parent_node_id 不存在或属于其他 Session: " f"{parent_id}"
                )

        moved_names: set[str] = set()
        for command in transaction.reference_moves:
            if command.reference_name in moved_names:
                raise StorageIntegrityError(
                    f"同一事务不能多次移动 Reference: {command.reference_name}"
                )
            moved_names.add(command.reference_name)
            current = self._find_named_reference_unlocked(
                session_id=transaction.session_id,
                reference_name=command.reference_name,
            )
            current_commit_sequence = (
                current.commit_sequence if current is not None else None
            )
            if current_commit_sequence != command.expected_current_commit_sequence:
                raise StorageConflictError(
                    f"NamedReference commit_sequence 冲突: {command.reference_name}; "
                    f"expected={command.expected_current_commit_sequence}, "
                    f"actual={current_commit_sequence}"
                )
            if command.target_kind == "object":
                exists = (
                    command.target_id in staged_object_ids
                    or command.target_id in self._objects
                )
            else:
                existing_node = self._nodes.get(command.target_id)
                exists = command.target_id in staged_node_ids or (
                    existing_node is not None
                    and existing_node.session_id == transaction.session_id
                )
            if not exists:
                raise StorageIntegrityError(
                    f"NamedReference 指向不存在的 {command.target_kind}: "
                    f"{command.target_id}"
                )

    @staticmethod
    def _ensure_acyclic_parent_chain(
        *,
        node_id: str,
        parent_by_node: dict[str, str | None],
    ) -> None:
        visited = {node_id}
        parent_id = parent_by_node[node_id]
        while parent_id is not None and parent_id in parent_by_node:
            if parent_id in visited:
                raise StorageIntegrityError(
                    "同一事务中的 ConversationNode 存在 parent 环"
                )
            visited.add(parent_id)
            parent_id = parent_by_node[parent_id]

    def _find_named_reference_unlocked(
        self,
        *,
        session_id: str,
        reference_name: str,
    ) -> NamedReference | None:
        versions = self._references.get((session_id, reference_name), [])
        return versions[-1] if versions else None

    @staticmethod
    def _copy_content(content: dict) -> dict:
        return json.loads(json.dumps(content, ensure_ascii=False))

    @classmethod
    def _copy_object(cls, value: ImmutableObject) -> ImmutableObject:
        return replace(value, content=cls._copy_content(value.content))
