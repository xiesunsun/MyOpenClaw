"""现有 Runtime Store 的 ModelCall v12 窄能力。"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

from pickel.conversations.conversation_node import ConversationNode
from pickel.model_calls.content import decode_request_content, decode_response_content
from pickel.model_calls.content_store import (
    FileModelCallContentStore,
    InMemoryModelCallContentStore,
    ModelCallContentRef,
    ModelCallContentStore,
)
from pickel.model_calls.model_call import (
    ModelCall,
    ModelCallError,
    ModelCallStatus,
)
from pickel.operations.agent_run_state import AgentRunState
from pickel.persistence.errors import StorageConflictError, StorageIntegrityError
from pickel.shared.execution_identity import ExecutionIdentity
from pickel.shared.frozen_json import thaw_json

_TERMINAL = frozenset({"completed", "failed", "cancelled", "incomplete"})


class InMemoryModelCallStoreMixin:
    """依赖现有 InMemoryRuntimeStore 字典与 RLock 的 ModelCall 事务。"""

    @property
    def model_call_content_store(self) -> ModelCallContentStore:
        store = getattr(self, "_model_call_content_store", None)
        if store is None:
            store = InMemoryModelCallContentStore()
            self._model_call_content_store = store
        return store

    def _model_call_rows(self) -> dict[str, ModelCall]:
        rows = getattr(self, "_model_calls", None)
        if rows is None:
            rows = {}
            self._model_calls = rows
        return rows

    def load_model_call(self, model_call_id: str) -> ModelCall | None:
        with self._lock:
            call = self._model_call_rows().get(model_call_id)
            if call is not None:
                _validate_content_refs(self.model_call_content_store, call)
            return call

    def list_model_calls(
        self,
        *,
        session_id: str,
        operation_id: str | None = None,
        step_id: str | None = None,
    ) -> tuple[ModelCall, ...]:
        with self._lock:
            values = [
                call
                for call in self._model_call_rows().values()
                if call.session_id == session_id
                and (operation_id is None or call.operation_id == operation_id)
                and (step_id is None or call.step_id == step_id)
            ]
            for call in values:
                _validate_content_refs(self.model_call_content_store, call)
        values.sort(
            key=lambda call: (call.request_attempt, call.created_at, call.model_call_id)
        )
        return tuple(values)

    def prepare_agent_model_call(
        self,
        *,
        model_call: ModelCall,
        state: AgentRunState,
        expected_revision: int,
        updated_at: datetime,
    ) -> bool:
        _require_request_content(self.model_call_content_store, model_call)
        with self._lock:
            if not _request_content_exists(self.model_call_content_store, model_call):
                raise StorageIntegrityError("ModelCall RequestContent 在事务前已缺失")
            current = self._run_states.get(state.operation_id)
            operation = self._operations.get(state.operation_id)
            if current is None or operation is None:
                return False
            if current.revision != expected_revision:
                return False
            session = self._sessions.get(operation.session_id)
            if session is None or session.active_operation_id != state.operation_id:
                return False
            _validate_prepared_agent_call(
                current=current,
                state=state,
                model_call=model_call,
                expected_revision=expected_revision,
                session_id=operation.session_id,
            )
            rows = self._model_call_rows()
            if model_call.model_call_id in rows:
                raise StorageConflictError(
                    f"ModelCall 已存在: {model_call.model_call_id}"
                )
            if any(
                call.operation_id == model_call.operation_id
                and call.step_id == model_call.step_id
                and call.request_attempt == model_call.request_attempt
                for call in rows.values()
                if call.operation_id is not None
            ):
                raise StorageConflictError(
                    "同一 operation/step/request_attempt 已存在 ModelCall"
                )
            self._run_states[state.operation_id] = state
            rows[model_call.model_call_id] = model_call
            return True

    def insert_session_model_call(self, *, model_call: ModelCall) -> None:
        _require_request_content(self.model_call_content_store, model_call)
        if model_call.operation_id is not None or model_call.status != "prepared":
            raise StorageIntegrityError(
                "Session ModelCall 必须是无 Operation 的 prepared 调用"
            )
        with self._lock:
            if model_call.session_id not in self._sessions:
                raise StorageIntegrityError("ModelCall Session 不存在")
            rows = self._model_call_rows()
            existing = rows.get(model_call.model_call_id)
            if existing is not None:
                if existing == model_call:
                    return
                raise StorageConflictError("ModelCall ID 已存在但内容不同")
            rows[model_call.model_call_id] = model_call

    def transition_model_call(
        self,
        *,
        model_call: ModelCall,
        expected_status: ModelCallStatus,
    ) -> bool:
        _validate_content_refs(self.model_call_content_store, model_call)
        with self._lock:
            rows = self._model_call_rows()
            current = rows.get(model_call.model_call_id)
            if current is None:
                return False
            if current == model_call:
                return True
            if current.status != expected_status:
                if current.terminal:
                    raise StorageConflictError("ModelCall 已进入冲突终态")
                return False
            _validate_transition(current, model_call)
            rows[model_call.model_call_id] = model_call
            return True

    def commit_agent_model_response(
        self,
        *,
        model_call: ModelCall,
        state: AgentRunState,
        expected_revision: int,
        node: ConversationNode,
        updated_at: datetime,
    ) -> bool:
        response = _require_complete_response(self.model_call_content_store, model_call)
        if response.assistant_message != node.content:
            raise StorageIntegrityError(
                "ResponseContent 与 AssistantMessage Node 不一致"
            )
        with self._lock:
            rows = self._model_call_rows()
            current_call = rows.get(model_call.model_call_id)
            current = self._run_states.get(state.operation_id)
            operation = self._operations.get(state.operation_id)
            if current_call is None or current is None or operation is None:
                return False
            if (
                current_call.status != "in_flight"
                or current.revision != expected_revision
            ):
                return False
            session = self._sessions.get(operation.session_id)
            if session is None or session.active_operation_id != state.operation_id:
                return False
            _validate_response_commit(
                current_call=current_call,
                model_call=model_call,
                current=current,
                state=state,
                expected_revision=expected_revision,
                session_id=operation.session_id,
                node=node,
            )
            if node.node_id in self._nodes:
                raise StorageIntegrityError(f"ConversationNode 已存在: {node.node_id}")
            if node.parent_node_id != session.active_node_id:
                return False
            self._validate_content_artifacts_unlocked(node.content)
            self._validate_state_references_unlocked(state, pending_node=node)
            if node.node_id not in self._state_node_ids(state):
                raise StorageIntegrityError(
                    "AssistantMessage Node 必须被新 AgentRunState 引用"
                )
            self._nodes[node.node_id] = node
            rows[model_call.model_call_id] = model_call
            self._run_states[state.operation_id] = state
            self._sessions[session.session_id] = replace(
                session,
                active_node_id=node.node_id,
                updated_at=updated_at,
            )
            return True

    def commit_agent_model_processing_failure(
        self,
        *,
        model_call: ModelCall,
        state: AgentRunState,
        expected_revision: int,
        node: ConversationNode,
        updated_at: datetime,
    ) -> bool:
        response = _require_complete_response(self.model_call_content_store, model_call)
        if response.assistant_message != node.content:
            raise StorageIntegrityError(
                "ResponseContent 与 AssistantMessage Node 不一致"
            )
        with self._lock:
            rows = self._model_call_rows()
            current_call = rows.get(model_call.model_call_id)
            current = self._run_states.get(state.operation_id)
            operation = self._operations.get(state.operation_id)
            if current_call is None or current is None or operation is None:
                return False
            if (
                current_call.status != "in_flight"
                or current.revision != expected_revision
            ):
                return False
            session = self._sessions.get(operation.session_id)
            if session is None or session.active_operation_id != state.operation_id:
                return False
            _validate_processing_failure(
                current_call=current_call,
                model_call=model_call,
                current=current,
                state=state,
                expected_revision=expected_revision,
                session_id=operation.session_id,
                node=node,
            )
            if node.node_id in self._nodes:
                raise StorageIntegrityError(f"ConversationNode 已存在: {node.node_id}")
            if node.parent_node_id != session.active_node_id:
                return False
            self._validate_content_artifacts_unlocked(node.content)
            self._validate_state_references_unlocked(state, pending_node=node)
            settled = self._build_settled_message_unlocked(
                child_session_id=session.session_id,
                state=state,
                node=node,
                created_at=updated_at,
            )
            self._nodes[node.node_id] = node
            rows[model_call.model_call_id] = model_call
            self._run_states[state.operation_id] = state
            self._sessions[session.session_id] = replace(
                session,
                active_node_id=node.node_id,
                active_operation_id=None,
                updated_at=updated_at,
            )
            if settled is not None:
                self._inbox[settled.message_id] = settled
            return True


class SQLiteModelCallStoreMixin:
    """依赖现有 SQLiteRuntimeStore 连接与领域校验器的 ModelCall 事务。"""

    @property
    def model_call_content_store(self) -> ModelCallContentStore:
        store = getattr(self, "_model_call_content_store", None)
        if store is None:
            root = Path(self._db_path).parent / "model-call-content"
            store = FileModelCallContentStore(root)
            self._model_call_content_store = store
        return store

    def load_model_call(self, model_call_id: str) -> ModelCall | None:
        self._ensure_schema()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM model_calls WHERE model_call_id = ?",
                (model_call_id,),
            ).fetchone()
        call = _model_call_from_row(row) if row is not None else None
        if call is not None:
            _validate_content_refs(self.model_call_content_store, call)
        return call

    def list_model_calls(
        self,
        *,
        session_id: str,
        operation_id: str | None = None,
        step_id: str | None = None,
    ) -> tuple[ModelCall, ...]:
        self._ensure_schema()
        query = "SELECT * FROM model_calls WHERE session_id = ?"
        args: list[Any] = [session_id]
        if operation_id is not None:
            query += " AND operation_id = ?"
            args.append(operation_id)
        if step_id is not None:
            query += " AND step_id = ?"
            args.append(step_id)
        query += " ORDER BY request_attempt, created_at, model_call_id"
        with self._connect() as connection:
            rows = connection.execute(query, args).fetchall()
        calls = tuple(_model_call_from_row(row) for row in rows)
        for call in calls:
            _validate_content_refs(self.model_call_content_store, call)
        return calls

    def prepare_agent_model_call(
        self,
        *,
        model_call: ModelCall,
        state: AgentRunState,
        expected_revision: int,
        updated_at: datetime,
    ) -> bool:
        _require_request_content(self.model_call_content_store, model_call)
        self._ensure_schema()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                if not _request_content_exists(
                    self.model_call_content_store, model_call
                ):
                    raise StorageIntegrityError(
                        "ModelCall RequestContent 在事务前已缺失"
                    )
                current_row = connection.execute(
                    "SELECT * FROM agent_run_states WHERE operation_id = ?",
                    (state.operation_id,),
                ).fetchone()
                operation = connection.execute(
                    "SELECT operation_id, session_id FROM session_operations "
                    "WHERE operation_id = ?",
                    (state.operation_id,),
                ).fetchone()
                if current_row is None or operation is None:
                    connection.rollback()
                    return False
                if int(current_row["revision"]) != expected_revision:
                    connection.rollback()
                    return False
                session = connection.execute(
                    "SELECT active_operation_id FROM conversation_sessions "
                    "WHERE session_id = ?",
                    (operation["session_id"],),
                ).fetchone()
                if (
                    session is None
                    or session["active_operation_id"] != state.operation_id
                ):
                    connection.rollback()
                    return False
                current = self._run_state_from_row(current_row)
                _validate_prepared_agent_call(
                    current=current,
                    state=state,
                    model_call=model_call,
                    expected_revision=expected_revision,
                    session_id=str(operation["session_id"]),
                )
                cursor = connection.execute(
                    """
                    UPDATE agent_run_states
                    SET revision = ?, current_step_json = ?, updated_at = ?
                    WHERE operation_id = ? AND revision = ?
                    """,
                    (
                        state.revision,
                        _json(state.current_step.content_dict()),
                        updated_at.isoformat(),
                        state.operation_id,
                        expected_revision,
                    ),
                )
                if cursor.rowcount != 1:
                    connection.rollback()
                    return False
                connection.execute(
                    _INSERT_MODEL_CALL_SQL, _model_call_values(model_call)
                )
                connection.commit()
                return True
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise StorageConflictError(
                    "ModelCall prepared 事务违反 SQLite 唯一性或引用约束"
                ) from exc
            except Exception:
                connection.rollback()
                raise

    def insert_session_model_call(self, *, model_call: ModelCall) -> None:
        _require_request_content(self.model_call_content_store, model_call)
        if model_call.operation_id is not None or model_call.status != "prepared":
            raise StorageIntegrityError(
                "Session ModelCall 必须是无 Operation 的 prepared 调用"
            )
        self._ensure_schema()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    "SELECT * FROM model_calls WHERE model_call_id = ?",
                    (model_call.model_call_id,),
                ).fetchone()
                if existing is not None:
                    if _model_call_from_row(existing) == model_call:
                        connection.commit()
                        return
                    raise StorageConflictError("ModelCall ID 已存在但内容不同")
                session = connection.execute(
                    "SELECT 1 FROM conversation_sessions WHERE session_id = ?",
                    (model_call.session_id,),
                ).fetchone()
                if session is None:
                    raise StorageIntegrityError("ModelCall Session 不存在")
                connection.execute(
                    _INSERT_MODEL_CALL_SQL, _model_call_values(model_call)
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def transition_model_call(
        self,
        *,
        model_call: ModelCall,
        expected_status: ModelCallStatus,
    ) -> bool:
        _validate_content_refs(self.model_call_content_store, model_call)
        self._ensure_schema()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM model_calls WHERE model_call_id = ?",
                    (model_call.model_call_id,),
                ).fetchone()
                if row is None:
                    connection.rollback()
                    return False
                current = _model_call_from_row(row)
                if current == model_call:
                    connection.commit()
                    return True
                if current.status != expected_status:
                    if current.terminal:
                        raise StorageConflictError("ModelCall 已进入冲突终态")
                    connection.rollback()
                    return False
                _validate_transition(current, model_call)
                cursor = connection.execute(
                    """
                    UPDATE model_calls
                    SET returned_model = ?, status = ?, response_content_ref = ?,
                        provider_request_id = ?, http_status = ?, error_json = ?,
                        started_at = ?, first_chunk_at = ?, finished_at = ?
                    WHERE model_call_id = ? AND status = ?
                    """,
                    (
                        model_call.returned_model,
                        model_call.status,
                        model_call.response_content_ref,
                        model_call.provider_request_id,
                        model_call.http_status,
                        _error_json(model_call.error),
                        _iso(model_call.started_at),
                        _iso(model_call.first_chunk_at),
                        _iso(model_call.finished_at),
                        model_call.model_call_id,
                        expected_status,
                    ),
                )
                if cursor.rowcount != 1:
                    connection.rollback()
                    return False
                connection.commit()
                return True
            except Exception:
                connection.rollback()
                raise

    def commit_agent_model_response(
        self,
        *,
        model_call: ModelCall,
        state: AgentRunState,
        expected_revision: int,
        node: ConversationNode,
        updated_at: datetime,
    ) -> bool:
        response = _require_complete_response(self.model_call_content_store, model_call)
        if response.assistant_message != node.content:
            raise StorageIntegrityError(
                "ResponseContent 与 AssistantMessage Node 不一致"
            )
        self._ensure_schema()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                call_row = connection.execute(
                    "SELECT * FROM model_calls WHERE model_call_id = ?",
                    (model_call.model_call_id,),
                ).fetchone()
                state_row = connection.execute(
                    "SELECT * FROM agent_run_states WHERE operation_id = ?",
                    (state.operation_id,),
                ).fetchone()
                operation = connection.execute(
                    "SELECT session_id FROM session_operations WHERE operation_id = ?",
                    (state.operation_id,),
                ).fetchone()
                if call_row is None or state_row is None or operation is None:
                    connection.rollback()
                    return False
                current_call = _model_call_from_row(call_row)
                current = self._run_state_from_row(state_row)
                if (
                    current_call.status != "in_flight"
                    or current.revision != expected_revision
                ):
                    connection.rollback()
                    return False
                session = connection.execute(
                    "SELECT * FROM conversation_sessions WHERE session_id = ?",
                    (operation["session_id"],),
                ).fetchone()
                if (
                    session is None
                    or session["active_operation_id"] != state.operation_id
                ):
                    connection.rollback()
                    return False
                _validate_response_commit(
                    current_call=current_call,
                    model_call=model_call,
                    current=current,
                    state=state,
                    expected_revision=expected_revision,
                    session_id=str(operation["session_id"]),
                    node=node,
                )
                if node.parent_node_id != _optional(session["active_node_id"]):
                    connection.rollback()
                    return False
                self._validate_node_artifacts(connection, node)
                if node.node_id not in _state_node_ids(state):
                    raise StorageIntegrityError(
                        "AssistantMessage Node 必须被新 AgentRunState 引用"
                    )
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
                call_cursor = connection.execute(
                    """
                    UPDATE model_calls
                    SET returned_model = ?, status = 'completed',
                        response_content_ref = ?, provider_request_id = ?,
                        http_status = ?, error_json = NULL,
                        started_at = ?, first_chunk_at = ?, finished_at = ?
                    WHERE model_call_id = ? AND status = 'in_flight'
                    """,
                    (
                        model_call.returned_model,
                        model_call.response_content_ref,
                        model_call.provider_request_id,
                        model_call.http_status,
                        _iso(model_call.started_at),
                        _iso(model_call.first_chunk_at),
                        _iso(model_call.finished_at),
                        model_call.model_call_id,
                    ),
                )
                if call_cursor.rowcount != 1:
                    connection.rollback()
                    return False
                state_cursor = connection.execute(
                    """
                    UPDATE agent_run_states
                    SET revision = ?, status = ?, waiting_reason = ?,
                        completed_step_count = ?, current_step_json = ?,
                        final_assistant_node_id = ?, error_json = ?,
                        cancellation_json = ?, updated_at = ?
                    WHERE operation_id = ? AND revision = ?
                    """,
                    _run_state_update_values(
                        state,
                        expected_revision=expected_revision,
                        updated_at=updated_at,
                    ),
                )
                if state_cursor.rowcount != 1:
                    connection.rollback()
                    return False
                session_cursor = connection.execute(
                    """
                    UPDATE conversation_sessions
                    SET active_node_id = ?, updated_at = ?
                    WHERE session_id = ? AND active_operation_id = ?
                    """,
                    (
                        node.node_id,
                        updated_at.isoformat(),
                        session["session_id"],
                        state.operation_id,
                    ),
                )
                if session_cursor.rowcount != 1:
                    connection.rollback()
                    return False
                connection.commit()
                return True
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise StorageIntegrityError("ModelCall 响应原子提交失败") from exc
            except Exception:
                connection.rollback()
                raise

    def commit_agent_model_processing_failure(
        self,
        *,
        model_call: ModelCall,
        state: AgentRunState,
        expected_revision: int,
        node: ConversationNode,
        updated_at: datetime,
    ) -> bool:
        response = _require_complete_response(self.model_call_content_store, model_call)
        if response.assistant_message != node.content:
            raise StorageIntegrityError(
                "ResponseContent 与 AssistantMessage Node 不一致"
            )
        self._ensure_schema()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                call_row = connection.execute(
                    "SELECT * FROM model_calls WHERE model_call_id = ?",
                    (model_call.model_call_id,),
                ).fetchone()
                state_row = connection.execute(
                    "SELECT * FROM agent_run_states WHERE operation_id = ?",
                    (state.operation_id,),
                ).fetchone()
                operation = connection.execute(
                    "SELECT session_id FROM session_operations WHERE operation_id = ?",
                    (state.operation_id,),
                ).fetchone()
                if call_row is None or state_row is None or operation is None:
                    connection.rollback()
                    return False
                current_call = _model_call_from_row(call_row)
                current = self._run_state_from_row(state_row)
                if (
                    current_call.status != "in_flight"
                    or current.revision != expected_revision
                ):
                    connection.rollback()
                    return False
                session = connection.execute(
                    "SELECT * FROM conversation_sessions WHERE session_id = ?",
                    (operation["session_id"],),
                ).fetchone()
                if (
                    session is None
                    or session["active_operation_id"] != state.operation_id
                ):
                    connection.rollback()
                    return False
                _validate_processing_failure(
                    current_call=current_call,
                    model_call=model_call,
                    current=current,
                    state=state,
                    expected_revision=expected_revision,
                    session_id=str(operation["session_id"]),
                    node=node,
                )
                if node.parent_node_id != _optional(session["active_node_id"]):
                    connection.rollback()
                    return False
                self._validate_node_artifacts(connection, node)
                if connection.execute(
                    "SELECT 1 FROM conversation_nodes WHERE node_id = ?",
                    (node.node_id,),
                ).fetchone():
                    raise StorageIntegrityError(
                        f"ConversationNode 已存在: {node.node_id}"
                    )
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
                call_cursor = connection.execute(
                    """
                    UPDATE model_calls
                    SET returned_model = ?, status = 'completed',
                        response_content_ref = ?, provider_request_id = ?,
                        http_status = ?, error_json = NULL,
                        started_at = ?, first_chunk_at = ?, finished_at = ?
                    WHERE model_call_id = ? AND status = 'in_flight'
                    """,
                    (
                        model_call.returned_model,
                        model_call.response_content_ref,
                        model_call.provider_request_id,
                        model_call.http_status,
                        _iso(model_call.started_at),
                        _iso(model_call.first_chunk_at),
                        _iso(model_call.finished_at),
                        model_call.model_call_id,
                    ),
                )
                if call_cursor.rowcount != 1:
                    connection.rollback()
                    return False
                state_cursor = connection.execute(
                    """
                    UPDATE agent_run_states
                    SET revision = ?, status = ?, waiting_reason = ?,
                        completed_step_count = ?, current_step_json = ?,
                        final_assistant_node_id = ?, error_json = ?,
                        cancellation_json = ?, updated_at = ?
                    WHERE operation_id = ? AND revision = ?
                    """,
                    _run_state_update_values(
                        state,
                        expected_revision=expected_revision,
                        updated_at=updated_at,
                    ),
                )
                if state_cursor.rowcount != 1:
                    connection.rollback()
                    return False
                session_cursor = connection.execute(
                    """
                    UPDATE conversation_sessions
                    SET active_node_id = ?, active_operation_id = NULL, updated_at = ?
                    WHERE session_id = ? AND active_operation_id = ?
                    """,
                    (
                        node.node_id,
                        updated_at.isoformat(),
                        operation["session_id"],
                        state.operation_id,
                    ),
                )
                if session_cursor.rowcount != 1:
                    connection.rollback()
                    return False
                self._insert_settled_message_in_transaction(
                    connection=connection,
                    child_session_id=str(operation["session_id"]),
                    state=state,
                    node=node,
                    created_at=updated_at,
                )
                connection.commit()
                return True
            except Exception:
                connection.rollback()
                raise


_INSERT_MODEL_CALL_SQL = """
INSERT INTO model_calls (
    model_call_id, session_id, operation_id, step_id, step_sequence,
    request_attempt, model_role, purpose, provider, api_kind, endpoint,
    requested_model, returned_model, status, request_content_ref,
    response_content_ref, context_fingerprint, provider_request_id,
    http_status, error_json, created_at, started_at, first_chunk_at, finished_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def _validate_prepared_agent_call(
    *,
    current: AgentRunState,
    state: AgentRunState,
    model_call: ModelCall,
    expected_revision: int,
    session_id: str,
) -> None:
    current_step = current.current_step
    if (
        current.status != "running"
        or current_step is None
        or current_step.phase != "request_ready"
        or current_step.request_intent is None
    ):
        raise StorageIntegrityError(
            "只有 running/request_ready AgentRunState 可以准备 ModelCall"
        )
    next_step = replace(current_step, request_attempt=current_step.request_attempt + 1)
    expected_state = replace(
        current,
        revision=expected_revision + 1,
        current_step=next_step,
    )
    if state != expected_state:
        raise StorageIntegrityError(
            "prepare_agent_model_call 只能递增 revision 和 request_attempt"
        )
    if (
        model_call.status != "prepared"
        or model_call.purpose != "agent_step"
        or model_call.model_role != "primary"
        or model_call.session_id != session_id
        or model_call.operation_id != state.operation_id
        or model_call.step_id != next_step.step_id
        or model_call.step_sequence != next_step.step_sequence
        or model_call.request_attempt != next_step.request_attempt
        or model_call.context_fingerprint
        != next_step.request_intent.context_fingerprint
    ):
        raise StorageIntegrityError("ModelCall 与 AgentRunState request attempt 不一致")


def _validate_response_commit(
    *,
    current_call: ModelCall,
    model_call: ModelCall,
    current: AgentRunState,
    state: AgentRunState,
    expected_revision: int,
    session_id: str,
    node: ConversationNode,
) -> None:
    _validate_transition(current_call, model_call, allow_completed=True)
    if model_call.status != "completed":
        raise StorageIntegrityError("响应事务只能提交 completed ModelCall")
    current_step = current.current_step
    next_step = state.current_step
    if (
        current.status != "running"
        or current_step is None
        or current_step.phase != "request_ready"
        or current_step.request_intent is None
        or next_step is None
        or next_step.phase != "awaiting_tools"
        or next_step.request_intent is not None
        or next_step.assistant_message_node_id != node.node_id
        or state.revision != expected_revision + 1
        or state.operation_id != current.operation_id
        or state.completed_step_count != current.completed_step_count
        or state.status not in {"running", "waiting"}
        or model_call.operation_id != current.operation_id
        or model_call.step_id != current_step.step_id
        or model_call.step_sequence != current_step.step_sequence
        or model_call.request_attempt != current_step.request_attempt
        or node.session_id != session_id
        or node.content_type != "agent_message"
    ):
        raise StorageIntegrityError("ModelCall 响应与 AgentRunState/Node 转换不一致")


def _validate_processing_failure(
    *,
    current_call: ModelCall,
    model_call: ModelCall,
    current: AgentRunState,
    state: AgentRunState,
    expected_revision: int,
    session_id: str,
    node: ConversationNode,
) -> None:
    """校验完整 Provider 响应与失败 Operation 的原子提交。"""
    _validate_transition(current_call, model_call, allow_completed=True)
    current_step = current.current_step
    if (
        current.status != "running"
        or current_step is None
        or current_step.phase != "request_ready"
        or current_step.request_intent is None
        or state.status != "failed"
        or state.current_step is not None
        or state.final_assistant_node_id is not None
        or state.revision != expected_revision + 1
        or state.operation_id != current.operation_id
        or state.error is None
        or model_call.status != "completed"
        or model_call.operation_id != current.operation_id
        or model_call.step_id != current_step.step_id
        or model_call.step_sequence != current_step.step_sequence
        or model_call.request_attempt != current_step.request_attempt
        or node.session_id != session_id
        or node.content_type != "agent_message"
    ):
        raise StorageIntegrityError(
            "ModelCall 失败处理响应与 AgentRunState/Node 转换不一致"
        )


def _validate_transition(
    current: ModelCall,
    target: ModelCall,
    *,
    allow_completed: bool = False,
) -> None:
    if current.model_call_id != target.model_call_id:
        raise StorageIntegrityError("ModelCall 状态转换不能改变 model_call_id")
    immutable = (
        "identity",
        "request_attempt",
        "model_role",
        "purpose",
        "provider",
        "api_kind",
        "endpoint",
        "requested_model",
        "request_content_ref",
        "context_fingerprint",
        "created_at",
    )
    if any(getattr(current, field) != getattr(target, field) for field in immutable):
        raise StorageIntegrityError("ModelCall 状态转换修改了不可变字段")
    allowed = {
        "prepared": {"in_flight", "cancelled"},
        "in_flight": {"failed", "cancelled", "incomplete"},
    }
    if allow_completed or current.operation_id is None:
        allowed["in_flight"] = {*allowed["in_flight"], "completed"}
    if target.status not in allowed.get(current.status, set()):
        raise StorageConflictError(
            f"非法 ModelCall 状态转换: {current.status} -> {target.status}"
        )


def _require_request_content(
    content_store: ModelCallContentStore,
    call: ModelCall,
) -> ModelCallContentRef:
    try:
        ref = ModelCallContentRef.from_string(call.request_content_ref)
    except (TypeError, ValueError) as exc:
        raise StorageIntegrityError("ModelCall RequestContent 引用无效") from exc
    try:
        decode_request_content(content_store.get(ref))
    except Exception as exc:
        raise StorageIntegrityError("ModelCall RequestContent 缺失或损坏") from exc
    return ref


def _request_content_exists(
    content_store: ModelCallContentStore,
    call: ModelCall,
) -> bool:
    try:
        ref = ModelCallContentRef.from_string(call.request_content_ref)
    except (TypeError, ValueError):
        return False
    if not content_store.exists(ref):
        return False
    try:
        decode_request_content(content_store.get(ref))
    except Exception:
        return False
    return True


def _require_complete_response(
    content_store: ModelCallContentStore,
    call: ModelCall,
):
    if call.status != "completed" or call.response_content_ref is None:
        raise StorageIntegrityError(
            "响应事务要求 completed ModelCall 和 ResponseContent"
        )
    try:
        ref = ModelCallContentRef.from_string(call.response_content_ref)
    except (TypeError, ValueError) as exc:
        raise StorageIntegrityError("ModelCall ResponseContent 引用无效") from exc
    try:
        response = decode_response_content(content_store.get(ref))
    except Exception as exc:
        raise StorageIntegrityError("ModelCall ResponseContent 缺失或损坏") from exc
    if response.partial:
        raise StorageIntegrityError(
            "completed ModelCall 不能引用 partial ResponseContent"
        )
    return response


def _validate_content_refs(
    content_store: ModelCallContentStore,
    call: ModelCall,
) -> None:
    _require_request_content(content_store, call)
    if call.response_content_ref is None:
        return
    try:
        ref = ModelCallContentRef.from_string(call.response_content_ref)
    except (TypeError, ValueError) as exc:
        raise StorageIntegrityError("ModelCall ResponseContent 引用无效") from exc
    try:
        response = decode_response_content(content_store.get(ref))
    except Exception as exc:
        raise StorageIntegrityError("ModelCall ResponseContent 缺失或损坏") from exc
    if call.status == "completed" and response.partial:
        raise StorageIntegrityError(
            "completed ModelCall 不能引用 partial ResponseContent"
        )


def _model_call_values(call: ModelCall) -> tuple[Any, ...]:
    return (
        call.model_call_id,
        call.session_id,
        call.operation_id,
        call.step_id,
        call.step_sequence,
        call.request_attempt,
        call.model_role,
        call.purpose,
        call.provider,
        call.api_kind,
        call.endpoint,
        call.requested_model,
        call.returned_model,
        call.status,
        call.request_content_ref,
        call.response_content_ref,
        call.context_fingerprint,
        call.provider_request_id,
        call.http_status,
        _error_json(call.error),
        call.created_at.isoformat(),
        _iso(call.started_at),
        _iso(call.first_chunk_at),
        _iso(call.finished_at),
    )


def _model_call_from_row(row: sqlite3.Row) -> ModelCall:
    error = None
    if row["error_json"] is not None:
        data = json.loads(str(row["error_json"]))
        if not isinstance(data, dict):
            raise StorageIntegrityError("ModelCall.error_json 不是 JSON object")
        details = data.get("details")
        if details is not None and not isinstance(details, dict):
            raise StorageIntegrityError("ModelCall.error.details 不是 JSON object")
        retryable = data.get("retryable")
        if retryable is not None and not isinstance(retryable, bool):
            raise StorageIntegrityError("ModelCall.error.retryable 不是 boolean")
        error = ModelCallError(
            code=str(data["code"]),
            message=str(data["message"]),
            retryable=retryable,
            details=details,
        )
    return ModelCall(
        model_call_id=str(row["model_call_id"]),
        identity=ExecutionIdentity(
            session_id=str(row["session_id"]),
            operation_id=_optional(row["operation_id"]),
            step_id=_optional(row["step_id"]),
            step_sequence=(
                int(row["step_sequence"]) if row["step_sequence"] is not None else None
            ),
        ),
        request_attempt=int(row["request_attempt"]),
        model_role=str(row["model_role"]),
        purpose=str(row["purpose"]),
        provider=str(row["provider"]),
        api_kind=str(row["api_kind"]),
        endpoint=str(row["endpoint"]),
        requested_model=str(row["requested_model"]),
        returned_model=_optional(row["returned_model"]),
        status=str(row["status"]),
        request_content_ref=str(row["request_content_ref"]),
        response_content_ref=_optional(row["response_content_ref"]),
        context_fingerprint=_optional(row["context_fingerprint"]),
        provider_request_id=_optional(row["provider_request_id"]),
        http_status=(
            int(row["http_status"]) if row["http_status"] is not None else None
        ),
        error=error,
        created_at=datetime.fromisoformat(str(row["created_at"])),
        started_at=_time(row["started_at"]),
        first_chunk_at=_time(row["first_chunk_at"]),
        finished_at=_time(row["finished_at"]),
    )


def _run_state_update_values(
    state: AgentRunState,
    *,
    expected_revision: int,
    updated_at: datetime,
) -> tuple[Any, ...]:
    return (
        state.revision,
        state.status,
        state.waiting_reason,
        state.completed_step_count,
        _json(state.current_step.content_dict() if state.current_step else None),
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
    )


def _state_node_ids(state: AgentRunState) -> set[str]:
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


def _error_json(error: ModelCallError | None) -> str | None:
    if error is None:
        return None
    return _json(
        {
            "code": error.code,
            "message": error.message,
            "retryable": error.retryable,
            "details": thaw_json(error.details) if error.details is not None else None,
        }
    )


def _json(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _time(value: Any) -> datetime | None:
    return datetime.fromisoformat(str(value)) if value is not None else None


def _optional(value: Any) -> str | None:
    return str(value) if value is not None else None
