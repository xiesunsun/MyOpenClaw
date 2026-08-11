"""SessionOperation 的原子接受、状态提交与恢复入口。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from uuid import uuid4

from pickel.conversations.agent_message import (
    AssistantMessage,
    ToolResultMessage,
    UserMessage,
    agent_message_from_dict,
    agent_message_to_dict,
)
from pickel.conversations.content_blocks import TextBlock, ToolCallBlock
from pickel.conversations.conversation_node import ConversationEntry
from pickel.conversations.conversation_service import ConversationNotFoundError
from pickel.operations.agent_run_state import (
    AgentRunState,
    ToolCallState,
    agent_run_state_from_content,
)
from pickel.operations.agent_delegation import AgentDelegation
from pickel.operations.operation_store import OperationStore
from pickel.operations.session_operation import SessionOperation
from pickel.persistence.storage_transaction import StorageIntegrityError
from pickel.runtime.operation_state_machine import OperationStateMachine

ACTIVE_CONVERSATION_REFERENCE = "conversation/active"
OPERATION_STATE_OBJECT_TYPE = "session_operation_state"


class SessionOperationNotFoundError(LookupError):
    pass


class UnfinishedAgentRunError(RuntimeError):
    """同一 ConversationSession 已有尚未结束的 AgentRun。"""

    def __init__(self, *, operation_id: str, status: str) -> None:
        self.operation_id = operation_id
        self.status = status
        super().__init__(
            "ConversationSession 已有未完成的 AgentRun: "
            f"{operation_id} ({status})；请先恢复或取消该 Operation，"
            "也可以创建新 Session"
        )


@dataclass(frozen=True)
class AcceptedAgentRun:
    operation: SessionOperation
    state: AgentRunState
    user_message_entry: ConversationEntry


@dataclass(frozen=True)
class AcceptedDelegatedAgentRun:
    accepted_run: AcceptedAgentRun
    delegation: AgentDelegation


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
        delegation_id_factory: Callable[[], str] | None = None,
        session_id_factory: Callable[[], str] | None = None,
        node_id_factory: Callable[[], str] | None = None,
        state_machine: OperationStateMachine | None = None,
    ) -> None:
        self._store = store
        self._operation_id_factory = operation_id_factory or (lambda: str(uuid4()))
        self._delegation_id_factory = delegation_id_factory or (lambda: str(uuid4()))
        self._session_id_factory = session_id_factory or (lambda: str(uuid4()))
        self._node_id_factory = node_id_factory or (lambda: str(uuid4()))
        self._state_machine = state_machine or OperationStateMachine()

    def accept_agent_run(
        self,
        *,
        session_id: str,
        agent_package_version_id: str,
        user_message: UserMessage,
        initial_model_context_feedback: tuple[str, ...] = (),
    ) -> AcceptedAgentRun:
        return self._accept_agent_run(
            session_id=session_id,
            agent_package_version_id=agent_package_version_id,
            user_message=user_message,
            initial_model_context_feedback=initial_model_context_feedback,
        )

    def accept_delegated_agent_run(
        self,
        *,
        session_id: str,
        agent_package_version_id: str,
        user_message: UserMessage,
        parent_operation_id: str,
        parent_step_id: str,
        parent_tool_call_id: str | None = None,
    ) -> AcceptedDelegatedAgentRun:
        parent_state = self.load_agent_run_state(parent_operation_id)
        parent_step = parent_state.current_step
        if parent_step is None or parent_step.step_id != parent_step_id:
            raise StorageIntegrityError(
                "AgentDelegation 必须关联父 Operation 的当前 ModelStep: "
                f"{parent_step_id}"
            )
        if parent_tool_call_id is not None and not any(
            tool_call.tool_call_id == parent_tool_call_id
            for tool_call in parent_step.tool_calls
        ):
            raise StorageIntegrityError(
                "AgentDelegation.parent_tool_call_id 不属于父 ModelStep: "
                f"{parent_tool_call_id}"
            )
        delegation_id = self._delegation_id_factory()
        accepted = self._accept_agent_run(
            session_id=session_id,
            agent_package_version_id=agent_package_version_id,
            user_message=user_message,
            delegation=(
                delegation_id,
                parent_operation_id,
                parent_step_id,
                parent_tool_call_id,
            ),
        )
        delegation = self._store.load_agent_delegation(delegation_id)
        if delegation is None:
            raise StorageIntegrityError(
                f"AgentDelegation 接受后不可见: {delegation_id}"
            )
        return AcceptedDelegatedAgentRun(
            accepted_run=accepted,
            delegation=delegation,
        )

    def start_delegated_run(
        self,
        *,
        agent_package_version_id: str,
        user_message: UserMessage,
        parent_operation_id: str,
        parent_step_id: str,
        parent_tool_call_id: str | None = None,
        cwd: str | None = None,
    ) -> AcceptedDelegatedAgentRun:
        """创建隔离 child Session，并原子接受其 AgentRun 与父子关系。"""
        package = self._store.load_agent_package_version(agent_package_version_id)
        if package is None:
            raise StorageIntegrityError(
                f"AgentPackageVersion 不存在: {agent_package_version_id}"
            )
        child_session_id = self._session_id_factory()
        self._store.create_conversation_session(
            session_id=child_session_id,
            agent_id=package.agent_id,
            cwd=cwd or package.definition.workspace_path,
        )
        try:
            return self.accept_delegated_agent_run(
                session_id=child_session_id,
                agent_package_version_id=agent_package_version_id,
                user_message=user_message,
                parent_operation_id=parent_operation_id,
                parent_step_id=parent_step_id,
                parent_tool_call_id=parent_tool_call_id,
            )
        except BaseException:
            # 接受失败时 child Session 尚无提交，可直接清理孤儿封面。
            self._store.delete_conversation_session(session_id=child_session_id)
            raise

    def _accept_agent_run(
        self,
        *,
        session_id: str,
        agent_package_version_id: str,
        user_message: UserMessage,
        initial_model_context_feedback: tuple[str, ...] = (),
        delegation: tuple[str, str, str, str | None] | None = None,
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
        unfinished = self.list_unfinished_agent_runs(session_id=session_id)
        if unfinished:
            operation, state = unfinished[0]
            raise UnfinishedAgentRunError(
                operation_id=operation.operation_id,
                status=state.status,
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
        if delegation is not None:
            (
                delegation_id,
                parent_operation_id,
                parent_step_id,
                parent_tool_call_id,
            ) = delegation
            transaction.create_agent_delegation(
                delegation_id=delegation_id,
                parent_operation_id=parent_operation_id,
                parent_step_id=parent_step_id,
                parent_tool_call_id=parent_tool_call_id,
                child_operation_id=operation_id,
            )
        user_object_id = transaction.insert_immutable_object(
            object_type="agent_message",
            schema_version=3,
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
            model_context_feedback=initial_model_context_feedback,
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

    def load_agent_delegation(self, delegation_id: str) -> AgentDelegation:
        delegation = self._store.load_agent_delegation(delegation_id)
        if delegation is None:
            raise LookupError(f"AgentDelegation 不存在: {delegation_id}")
        return delegation

    def list_agent_delegations(
        self,
        *,
        parent_operation_id: str,
    ) -> list[AgentDelegation]:
        self.load_session_operation(parent_operation_id)
        return self._store.list_agent_delegations(
            parent_operation_id=parent_operation_id
        )

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

    def record_reconciled_tool_result(
        self,
        *,
        operation_id: str,
        result_message: ToolResultMessage,
    ) -> AgentRunProgressCommit:
        """原子记录 Host 已核实的未知 ToolCall 结果，并解除 waiting。"""
        state = self.load_agent_run_state(operation_id)
        result_node_id = self._node_id_factory()
        next_state = self._state_machine.record_reconciled_tool_call(
            state,
            tool_call_id=result_message.tool_call_id,
            result_message_node_id=result_node_id,
            is_error=result_message.is_error,
        )
        return self.commit_agent_run_state(
            state=next_state,
            appended_message=result_message,
            appended_message_node_id=result_node_id,
        )

    def cancel_agent_run(self, *, operation_id: str, reason: str) -> AgentRunState:
        state = self._close_pending_tool_calls(
            operation_id=operation_id,
            result_text=f"工具执行被终止：{reason}",
        )
        next_state = self._state_machine.cancel_agent_run(state, reason=reason)
        return self.commit_agent_run_state(state=next_state).state

    def fail_agent_run(
        self,
        *,
        operation_id: str,
        error_type: str,
        message: str,
    ) -> AgentRunState:
        state = self._close_pending_tool_calls(
            operation_id=operation_id,
            result_text=f"工具执行因 AgentRun 异常而终止：{error_type}: {message}",
        )
        next_state = self._state_machine.fail_agent_run(
            state,
            error_type=error_type,
            message=message,
        )
        return self.commit_agent_run_state(state=next_state).state

    def _close_pending_tool_calls(
        self,
        *,
        operation_id: str,
        result_text: str,
    ) -> AgentRunState:
        """为所有悬空 ToolCall 写入错误结果，不执行或重放真实工具。"""
        state = self.load_agent_run_state(operation_id)
        step = state.current_step
        if step is None:
            return state
        if step.phase == "model_request_completed" and not step.tool_calls:
            if step.assistant_message_node_id is None:
                raise StorageIntegrityError(
                    "model_request_completed 缺少 AssistantMessage 节点"
                )
            operation = self.load_session_operation(operation_id)
            assistant_entry = self._find_active_entry(
                session_id=operation.session_id,
                node_id=step.assistant_message_node_id,
            )
            assistant_message = agent_message_from_dict(assistant_entry.object.content)
            if not isinstance(assistant_message, AssistantMessage):
                raise StorageIntegrityError(
                    "ModelStep.assistant_message_node_id 未引用 AssistantMessage"
                )
            persisted_tool_calls = tuple(
                ToolCallState(
                    tool_call_id=block.id,
                    tool_name=block.name,
                    arguments=dict(block.arguments),
                    execution_state="ready",
                )
                for block in assistant_message.content
                if isinstance(block, ToolCallBlock)
            )
            if persisted_tool_calls:
                state = self.commit_agent_run_state(
                    state=self._state_machine.prepare_tool_calls(
                        state,
                        tool_calls=persisted_tool_calls,
                    )
                ).state
                step = state.current_step
                assert step is not None
        for pending_call in tuple(step.tool_calls):
            if pending_call.execution_state == "completed":
                continue
            result_node_id = self._node_id_factory()
            next_state = self._state_machine.record_tool_call_aborted(
                state,
                tool_call_id=pending_call.tool_call_id,
                result_message_node_id=result_node_id,
            )
            state = self.commit_agent_run_state(
                state=next_state,
                appended_message=ToolResultMessage(
                    tool_call_id=pending_call.tool_call_id,
                    tool_name=pending_call.tool_name,
                    content=[TextBlock(text=result_text)],
                    is_error=True,
                ),
                appended_message_node_id=result_node_id,
            ).state
        return state

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
                schema_version=3,
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
