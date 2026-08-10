"""原子写入 Object、Node 和 Reference 的事务命令。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from uuid import uuid4

from pickel.persistence.named_reference import ReferenceTargetKind


class StorageConflictError(RuntimeError):
    """调用方读取的 Session 或 Reference 版本已经过期。"""


class StorageIntegrityError(RuntimeError):
    """事务命令违反持久化不变量。"""


@dataclass(frozen=True)
class StorageCommit:
    session_id: str
    commit_sequence: int
    commit_id: str
    committed_at: datetime


@dataclass(frozen=True)
class _InsertObject:
    object_id: str
    object_type: str
    schema_version: int
    content: dict[str, Any]


@dataclass(frozen=True)
class _AppendNode:
    node_id: str
    object_id: str
    parent_node_id: str | None


@dataclass(frozen=True)
class _MoveReference:
    reference_name: str
    target_kind: ReferenceTargetKind
    target_id: str
    expected_current_commit_sequence: int | None


@dataclass(frozen=True)
class _CreateSessionOperation:
    operation_id: str
    operation_type: str
    agent_package_version_id: str


@dataclass(frozen=True)
class _CreateAgentDelegation:
    delegation_id: str
    parent_operation_id: str
    parent_step_id: str
    parent_tool_call_id: str | None
    child_operation_id: str


class _StorageTransactionCommitter(Protocol):
    def _commit_storage_transaction(
        self,
        transaction: "StorageTransaction",
    ) -> StorageCommit: ...


class StorageTransaction:
    """收集一次原子提交的命令；实例只能提交一次。"""

    def __init__(
        self,
        *,
        store: _StorageTransactionCommitter,
        session_id: str,
        expected_commit_sequence: int,
    ) -> None:
        if expected_commit_sequence < 0:
            raise ValueError("expected_commit_sequence 不能小于 0")
        self.store = store
        self.session_id = session_id
        self.expected_commit_sequence = expected_commit_sequence
        self.object_inserts: list[_InsertObject] = []
        self.node_appends: list[_AppendNode] = []
        self.reference_moves: list[_MoveReference] = []
        self.operation_creates: list[_CreateSessionOperation] = []
        self.delegation_creates: list[_CreateAgentDelegation] = []
        self._committed = False

    def insert_immutable_object(
        self,
        *,
        object_type: str,
        content: dict[str, Any],
        schema_version: int = 1,
        object_id: str | None = None,
    ) -> str:
        self._ensure_open()
        if not object_type:
            raise ValueError("object_type 不能为空")
        if schema_version < 1:
            raise ValueError("schema_version 必须大于 0")
        if not isinstance(content, dict):
            raise TypeError("ImmutableObject.content 必须是 JSON object")
        try:
            copied = json.loads(json.dumps(content, ensure_ascii=False))
        except (TypeError, ValueError) as exc:
            raise TypeError("ImmutableObject.content 必须可 JSON 序列化") from exc
        resolved_id = object_id or str(uuid4())
        self.object_inserts.append(
            _InsertObject(
                object_id=resolved_id,
                object_type=object_type,
                schema_version=schema_version,
                content=copied,
            )
        )
        return resolved_id

    def append_conversation_node(
        self,
        *,
        object_id: str,
        parent_node_id: str | None,
        node_id: str | None = None,
    ) -> str:
        self._ensure_open()
        resolved_id = node_id or str(uuid4())
        self.node_appends.append(
            _AppendNode(
                node_id=resolved_id,
                object_id=object_id,
                parent_node_id=parent_node_id,
            )
        )
        return resolved_id

    def move_named_reference(
        self,
        *,
        reference_name: str,
        target_kind: ReferenceTargetKind,
        target_id: str,
        expected_current_commit_sequence: int | None,
    ) -> None:
        self._ensure_open()
        if not reference_name:
            raise ValueError("reference_name 不能为空")
        if target_kind not in {"node", "object"}:
            raise ValueError(f"不支持的 target_kind: {target_kind}")
        self.reference_moves.append(
            _MoveReference(
                reference_name=reference_name,
                target_kind=target_kind,
                target_id=target_id,
                expected_current_commit_sequence=expected_current_commit_sequence,
            )
        )

    def create_session_operation(
        self,
        *,
        operation_id: str,
        operation_type: str,
        agent_package_version_id: str,
    ) -> None:
        """把 Operation 身份加入本次接受事务。"""
        self._ensure_open()
        if not operation_id:
            raise ValueError("operation_id 不能为空")
        if operation_type != "agent_run":
            raise ValueError(f"不支持的 operation_type: {operation_type}")
        if not agent_package_version_id:
            raise ValueError("agent_package_version_id 不能为空")
        self.operation_creates.append(
            _CreateSessionOperation(
                operation_id=operation_id,
                operation_type=operation_type,
                agent_package_version_id=agent_package_version_id,
            )
        )

    def create_agent_delegation(
        self,
        *,
        delegation_id: str,
        parent_operation_id: str,
        parent_step_id: str,
        parent_tool_call_id: str | None,
        child_operation_id: str,
    ) -> None:
        """把父子关系与 child SessionOperation 放入同一接受事务。"""
        self._ensure_open()
        if not delegation_id:
            raise ValueError("delegation_id 不能为空")
        if not parent_operation_id or not child_operation_id:
            raise ValueError("父子 operation_id 不能为空")
        if parent_operation_id == child_operation_id:
            raise ValueError("AgentDelegation 不能指向自身")
        if not parent_step_id:
            raise ValueError("parent_step_id 不能为空")
        self.delegation_creates.append(
            _CreateAgentDelegation(
                delegation_id=delegation_id,
                parent_operation_id=parent_operation_id,
                parent_step_id=parent_step_id,
                parent_tool_call_id=parent_tool_call_id,
                child_operation_id=child_operation_id,
            )
        )

    def commit(self) -> StorageCommit:
        self._ensure_open()
        self._committed = True
        return self.store._commit_storage_transaction(self)

    def _ensure_open(self) -> None:
        if self._committed:
            raise RuntimeError("StorageTransaction 已提交，不能再次使用")
