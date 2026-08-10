"""SessionOperation 的原子接受、状态提交与恢复入口。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from uuid import uuid4

from pickel.conversations.agent_message import (
    AssistantMessage,
    ToolResultMessage,
    UserMessage,
    agent_message_to_dict,
)
from pickel.conversations.conversation_node import ConversationEntry
from pickel.conversations.conversation_service import ConversationNotFoundError
from pickel.operations.agent_run_state import (
    AgentRunState,
    agent_run_state_from_content,
)
from pickel.operations.operation_store import OperationStore
from pickel.operations.session_operation import SessionOperation
from pickel.persistence.storage_transaction import StorageIntegrityError
from pickel.runtime.operation_state_machine import OperationStateMachine

ACTIVE_CONVERSATION_REFERENCE = "conversation/active"
OPERATION_STATE_OBJECT_TYPE = "session_operation_state"


class SessionOperationNotFoundError(LookupError):
    pass


@dataclass(frozen=True)
class AcceptedAgentRun:
    operation: SessionOperation
    state: AgentRunState
    user_message_entry: ConversationEntry


@dataclass(frozen=True)
class AgentRunProgressCommit:
    state: AgentRunState
    appended_message_entry: ConversationEntry | None


class OperationService:
    """唯一持久化 Operation 身份和最新恢复状态的领域服务。"""

    def __init__(
        self,
        store: OperationStore,
        *,
        operation_id_factory: Callable[[], str] | None = None,
        node_id_factory: Callable[[], str] | None = None,
        state_machine: OperationStateMachine | None = None,
    ) -> None:
        self._store = store
        self._operation_id_factory = operation_id_factory or (lambda: str(uuid4()))
        self._node_id_factory = node_id_factory or (lambda: str(uuid4()))
        self._state_machine = state_machine or OperationStateMachine()

    def accept_agent_run(
        self,
        *,
        session_id: str,
        agent_package_version_id: str,
        user_message: UserMessage,
    ) -> AcceptedAgentRun:
        session = self._store.load_conversation_session(session_id)
        if session is None:
            raise ConversationNotFoundError(f"ConversationSession 不存在: {session_id}")
        package = self._store.load_agent_package_version(agent_package_version_id)
        if package is None:
            raise StorageIntegrityError(
                f"AgentPackageVersion 不存在: {agent_package_version_id}"
            )
        if package.agent_id != session.agent_id:
            raise StorageIntegrityError(
                "AgentPackageVersion 与 ConversationSession.agent_id 不匹配: "
                f"{package.agent_id} != {session.agent_id}"
            )

        operation_id = self._operation_id_factory()
        user_message_node_id = self._node_id_factory()
        active_reference = self._store.find_named_reference(
            session_id=session_id,
            reference_name=ACTIVE_CONVERSATION_REFERENCE,
        )
        transaction = self._store.begin_storage_transaction(
            session_id=session_id,
            expected_commit_sequence=session.current_commit_sequence,
        )
        transaction.create_session_operation(
            operation_id=operation_id,
            operation_type="agent_run",
            agent_package_version_id=agent_package_version_id,
        )
        user_object_id = transaction.insert_immutable_object(
            object_type="agent_message",
            schema_version=2,
            content=agent_message_to_dict(user_message),
        )
        transaction.append_conversation_node(
            node_id=user_message_node_id,
            object_id=user_object_id,
            parent_node_id=(
                active_reference.target_id if active_reference is not None else None
            ),
        )
        transaction.move_named_reference(
            reference_name=ACTIVE_CONVERSATION_REFERENCE,
            target_kind="node",
            target_id=user_message_node_id,
            expected_current_commit_sequence=(
                active_reference.commit_sequence
                if active_reference is not None
                else None
            ),
        )
        initial_state = self._state_machine.create_initial_agent_run_state(
            operation_id=operation_id,
            user_message_node_id=user_message_node_id,
        )
        state_object_id = transaction.insert_immutable_object(
            object_type=OPERATION_STATE_OBJECT_TYPE,
            schema_version=1,
            content=initial_state.content_dict(),
        )
        transaction.move_named_reference(
            reference_name=operation_state_reference_name(operation_id),
            target_kind="object",
            target_id=state_object_id,
            expected_current_commit_sequence=None,
        )
        transaction.commit()
        operation = self.load_session_operation(operation_id)
        state = self.load_agent_run_state(operation_id)
        user_entry = self._find_active_entry(
            session_id=session_id,
            node_id=user_message_node_id,
        )
        return AcceptedAgentRun(
            operation=operation,
            state=state,
            user_message_entry=user_entry,
        )

    def load_session_operation(self, operation_id: str) -> SessionOperation:
        operation = self._store.load_session_operation(operation_id)
        if operation is None:
            raise SessionOperationNotFoundError(
                f"SessionOperation 不存在: {operation_id}"
            )
        return operation

    def load_agent_run_state(self, operation_id: str) -> AgentRunState:
        operation = self.load_session_operation(operation_id)
        reference = self._store.find_named_reference(
            session_id=operation.session_id,
            reference_name=operation_state_reference_name(operation_id),
        )
        if reference is None or reference.target_kind != "object":
            raise StorageIntegrityError(
                f"OperationStateReference 不存在或目标错误: {operation_id}"
            )
        immutable_object = self._store.load_immutable_object(reference.target_id)
        if (
            immutable_object is None
            or immutable_object.object_type != OPERATION_STATE_OBJECT_TYPE
            or immutable_object.schema_version != 1
        ):
            raise StorageIntegrityError(
                f"SessionOperationState 不存在或类型错误: {operation_id}"
            )
        try:
            state = agent_run_state_from_content(immutable_object.content)
        except (TypeError, ValueError, KeyError) as exc:
            raise StorageIntegrityError(
                f"AgentRunState 内容损坏: {operation_id}: {exc}"
            ) from exc
        if state.operation_id != operation_id:
            raise StorageIntegrityError(
                f"AgentRunState.operation_id 不匹配: {state.operation_id}"
            )
        return state

    def list_unfinished_agent_runs(
        self,
        *,
        session_id: str,
    ) -> list[tuple[SessionOperation, AgentRunState]]:
        result: list[tuple[SessionOperation, AgentRunState]] = []
        for operation in self._store.list_session_operations(session_id=session_id):
            state = self.load_agent_run_state(operation.operation_id)
            if state.status not in {"succeeded", "failed", "cancelled"}:
                result.append((operation, state))
        return result

    def commit_agent_run_state(
        self,
        *,
        state: AgentRunState,
        appended_message: AssistantMessage | ToolResultMessage | None = None,
        appended_message_node_id: str | None = None,
    ) -> AgentRunProgressCommit:
        operation = self.load_session_operation(state.operation_id)
        current_state = self.load_agent_run_state(state.operation_id)
        self._state_machine.validate_agent_run_transition(
            current=current_state,
            next_state=state,
        )
        if (appended_message is None) != (appended_message_node_id is None):
            raise ValueError(
                "appended_message 与 appended_message_node_id 必须同时提供"
            )

        session = self._store.load_conversation_session(operation.session_id)
        if session is None:
            raise StorageIntegrityError(
                f"ConversationSession 不存在: {operation.session_id}"
            )
        self._validate_state_node_references(
            session_id=operation.session_id,
            state=state,
            appended_message_node_id=appended_message_node_id,
        )
        state_reference_name = operation_state_reference_name(operation.operation_id)
        state_reference = self._store.find_named_reference(
            session_id=operation.session_id,
            reference_name=state_reference_name,
        )
        if state_reference is None:
            raise StorageIntegrityError(
                f"OperationStateReference 不存在: {operation.operation_id}"
            )
        transaction = self._store.begin_storage_transaction(
            session_id=operation.session_id,
            expected_commit_sequence=session.current_commit_sequence,
        )

        if appended_message is not None:
            assert appended_message_node_id is not None
            active_reference = self._store.find_named_reference(
                session_id=operation.session_id,
                reference_name=ACTIVE_CONVERSATION_REFERENCE,
            )
            message_object_id = transaction.insert_immutable_object(
                object_type="agent_message",
                schema_version=2,
                content=agent_message_to_dict(appended_message),
            )
            transaction.append_conversation_node(
                node_id=appended_message_node_id,
                object_id=message_object_id,
                parent_node_id=(
                    active_reference.target_id if active_reference is not None else None
                ),
            )
            transaction.move_named_reference(
                reference_name=ACTIVE_CONVERSATION_REFERENCE,
                target_kind="node",
                target_id=appended_message_node_id,
                expected_current_commit_sequence=(
                    active_reference.commit_sequence
                    if active_reference is not None
                    else None
                ),
            )

        state_object_id = transaction.insert_immutable_object(
            object_type=OPERATION_STATE_OBJECT_TYPE,
            schema_version=1,
            content=state.content_dict(),
        )
        transaction.move_named_reference(
            reference_name=state_reference_name,
            target_kind="object",
            target_id=state_object_id,
            expected_current_commit_sequence=state_reference.commit_sequence,
        )
        transaction.commit()

        appended_entry = None
        if appended_message_node_id is not None:
            appended_entry = self._find_active_entry(
                session_id=operation.session_id,
                node_id=appended_message_node_id,
            )
        return AgentRunProgressCommit(
            state=self.load_agent_run_state(operation.operation_id),
            appended_message_entry=appended_entry,
        )

    def _find_active_entry(
        self,
        *,
        session_id: str,
        node_id: str,
    ) -> ConversationEntry:
        for entry in reversed(
            self._store.list_active_branch_entries(session_id=session_id)
        ):
            if entry.node.node_id == node_id:
                return entry
        raise StorageIntegrityError(f"活动分支中找不到 ConversationNode: {node_id}")

    def _validate_state_node_references(
        self,
        *,
        session_id: str,
        state: AgentRunState,
        appended_message_node_id: str | None,
    ) -> None:
        referenced_node_ids = {state.user_message_node_id}
        if state.final_assistant_node_id is not None:
            referenced_node_ids.add(state.final_assistant_node_id)
        if state.current_step is not None:
            if state.current_step.assistant_message_node_id is not None:
                referenced_node_ids.add(state.current_step.assistant_message_node_id)
            referenced_node_ids.update(
                tool_call.result_message_node_id
                for tool_call in state.current_step.tool_calls
                if tool_call.result_message_node_id is not None
            )
        existing_node_ids = {
            entry.node.node_id
            for entry in self._store.list_active_branch_entries(session_id=session_id)
        }
        allowed_node_ids = set(existing_node_ids)
        if appended_message_node_id is not None:
            allowed_node_ids.add(appended_message_node_id)
            if appended_message_node_id not in referenced_node_ids:
                raise StorageIntegrityError(
                    "新消息节点必须被提交后的 AgentRunState 引用"
                )
        missing = referenced_node_ids - allowed_node_ids
        if missing:
            raise StorageIntegrityError(
                "AgentRunState 引用了活动分支之外的 ConversationNode: "
                + ", ".join(sorted(missing))
            )


def operation_state_reference_name(operation_id: str) -> str:
    return f"operation/{operation_id}/state"
