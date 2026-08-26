"""Delegation 的 durable acceptance 事务。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from typing import Literal, Protocol
from uuid import uuid4

from pickel.agents.agent_package import AgentPackageVersion
from pickel.conversations.agent_message import AssistantMessage, UserMessage
from pickel.conversations.content_blocks import TextBlock
from pickel.conversations.conversation_session import ConversationSession
from pickel.inbox.message import AgentMessageSource, InboxMessage
from pickel.operations.agent_delegation import AgentDelegation
from pickel.operations.agent_run_state import (
    AgentRunError,
    AgentRunState,
    AgentRunStatus,
    DelegateAgentIntent,
    WaitingReason,
)
from pickel.operations.session_operation import SessionOperation
from pickel.persistence.errors import StorageConflictError, StorageIntegrityError
from pickel.workspaces.workspace import Workspace


class DelegationStore(Protocol):
    """Delegation acceptance 所需的最小持久化端口。"""

    def load_operation(self, operation_id: str) -> SessionOperation | None: ...

    def load_run_state(self, operation_id: str) -> AgentRunState | None: ...

    def load_session(self, session_id: str) -> ConversationSession | None: ...

    def load_workspace(self, workspace_id: str) -> Workspace | None: ...

    def load_agent_package_version(
        self, package_version_id: str
    ) -> AgentPackageVersion | None: ...

    def load_delegation(self, child_session_id: str) -> AgentDelegation | None: ...

    def list_operations(self, *, session_id: str) -> tuple[SessionOperation, ...]: ...

    def list_delegations(
        self, *, parent_operation_id: str
    ) -> tuple[AgentDelegation, ...]: ...

    def list_pending(
        self, *, session_id: str, delivery: str | None = None
    ) -> tuple[InboxMessage, ...]: ...

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
    ) -> AgentDelegation: ...

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
    ) -> InboxMessage: ...

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
    ) -> InboxMessage: ...

    def prepare_interrupt_agent(
        self,
        *,
        sender_operation_id: str,
        sender_step_id: str,
        sender_tool_call_id: str,
        target_child_session_id: str,
        handled_at: datetime,
    ) -> str | None: ...

    def prepare_cancel_delegation(
        self,
        *,
        sender_operation_id: str,
        sender_step_id: str,
        sender_tool_call_id: str,
        target_child_session_id: str,
        handled_at: datetime,
    ) -> str | None: ...

    def load_node(self, node_id: str): ...


class DelegationService:
    """校验父 ToolCall 并把 child Session 的三项事实一次接受。"""

    def __init__(
        self,
        *,
        store: DelegationStore,
        child_session_id_factory: Callable[[], str] | None = None,
        message_id_factory: Callable[[], str] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._child_session_id = child_session_id_factory or (lambda: str(uuid4()))
        self._message_id = message_id_factory or (lambda: str(uuid4()))
        self._now = now or (lambda: datetime.now(timezone.utc))

    def start_delegation(
        self,
        parent_operation_id: str,
        parent_step_id: str,
        parent_tool_call_id: str,
        message: UserMessage,
    ) -> AgentDelegation:
        operation = self._store.load_operation(parent_operation_id)
        if operation is None:
            raise StorageIntegrityError("parent Operation 不存在")
        if operation.session_id == "":
            raise StorageIntegrityError("parent Operation 的 session_id 不能为空")
        state = self._store.load_run_state(parent_operation_id)
        if state is None:
            raise StorageIntegrityError("parent Operation 的 AgentRunState 不存在")
        if state.status != "running":
            raise StorageConflictError("parent Operation 必须处于 running")
        step = state.current_step
        if (
            step is None
            or step.step_id != parent_step_id
            or step.phase != "awaiting_tools"
        ):
            raise StorageConflictError("parent ToolCall 不属于当前 awaiting_tools Step")
        call = next(
            (
                item
                for item in step.tool_calls
                if item.tool_call_id == parent_tool_call_id
            ),
            None,
        )
        if call is None or call.status != "intent_recorded":
            raise StorageConflictError("parent ToolCall 必须处于 intent_recorded")
        intent = call.execution_intent
        if not isinstance(intent, DelegateAgentIntent):
            raise StorageConflictError("parent ToolCall 没有 DelegateAgentIntent")

        parent_session = self._store.load_session(operation.session_id)
        if parent_session is None:
            raise StorageIntegrityError("parent Session 不存在")
        if parent_session.active_operation_id != parent_operation_id:
            raise StorageConflictError("parent Session 未指向 parent Operation")
        if (
            operation.workspace_binding.workspace_id != parent_session.workspace_id
            or operation.workspace_binding.working_directory != parent_session.cwd
        ):
            raise StorageIntegrityError("parent Operation.workspace_binding 漂移")
        workspace = self._store.load_workspace(parent_session.workspace_id)
        if workspace is None:
            raise StorageIntegrityError("parent Session 的 Workspace 不存在")
        parent_package = self._store.load_agent_package_version(
            operation.agent_package_version_id
        )
        child_package = self._store.load_agent_package_version(
            intent.child_package_version_id
        )
        if parent_package is None or child_package is None:
            raise StorageIntegrityError("parent/child AgentPackageVersion 不存在")
        depth = self._delegation_depth(operation.session_id)
        if depth >= parent_package.runtime_policy.max_delegation_depth:
            raise StorageConflictError("已达到 AgentPackage 的最大 delegation depth")

        now = self._now()
        child_session_id = self._child_session_id()
        message_id = self._message_id()
        source = AgentMessageSource(
            sender_session_id=operation.session_id,
            sender_operation_id=operation.operation_id,
            form="followup",
        )
        child_session = ConversationSession(
            session_id=child_session_id,
            agent_id=child_package.agent_id,
            workspace_id=parent_session.workspace_id,
            cwd=parent_session.cwd,
            active_node_id=None,
            active_operation_id=None,
            title=None,
            title_source=None,
            created_at=now,
            updated_at=now,
            archived_at=None,
        )
        delegation = AgentDelegation(
            child_session_id=child_session_id,
            parent_operation_id=parent_operation_id,
            parent_step_id=parent_step_id,
            parent_tool_call_id=parent_tool_call_id,
            initial_message_id=message_id,
            created_at=now,
        )
        return self._store.start_delegation(
            parent_operation_id=parent_operation_id,
            parent_step_id=parent_step_id,
            parent_tool_call_id=parent_tool_call_id,
            child_session=child_session,
            delegation=delegation,
            message_id=message_id,
            message=message,
            source=source,
            created_at=now,
        )

    def _delegation_depth(self, session_id: str) -> int:
        depth = 0
        seen: set[str] = set()
        while session_id not in seen:
            seen.add(session_id)
            delegation = self._store.load_delegation(session_id)
            if delegation is None:
                return depth
            depth += 1
            operation = self._store.load_operation(delegation.parent_operation_id)
            if operation is None:
                raise StorageIntegrityError("delegation parent Operation 不存在")
            session_id = operation.session_id
        raise StorageIntegrityError("delegation parent 链存在环")

    def send_parent_followup(
        self,
        sender_operation_id: str,
        sender_step_id: str,
        sender_tool_call_id: str,
        target_child_session_id: str,
        message: UserMessage,
    ) -> InboxMessage:
        """以稳定消息身份向当前 Operation 的 direct child 追加 followup。"""
        if not sender_operation_id or not sender_step_id or not sender_tool_call_id:
            raise ValueError("sender Operation、Step 和 ToolCall 身份不能为空")
        if not target_child_session_id:
            raise ValueError("child_session_id 不能为空")
        message_id = _followup_message_id(
            sender_operation_id, sender_step_id, sender_tool_call_id
        )
        operation = self._store.load_operation(sender_operation_id)
        if operation is None:
            raise StorageIntegrityError("sender Operation 不存在")
        source = AgentMessageSource(
            sender_session_id=operation.session_id,
            sender_operation_id=sender_operation_id,
            form="followup",
        )
        return self._store.send_parent_followup(
            sender_operation_id=sender_operation_id,
            sender_step_id=sender_step_id,
            sender_tool_call_id=sender_tool_call_id,
            target_child_session_id=target_child_session_id,
            message_id=message_id,
            message=message,
            source=source,
            created_at=self._now(),
        )

    def prepare_interrupt_agent(
        self,
        sender_operation_id: str,
        sender_step_id: str,
        sender_tool_call_id: str,
        target_child_session_id: str,
    ) -> str | None:
        """原子准备 direct child 中断，并返回当时的 active Operation。"""
        if not sender_operation_id or not sender_step_id or not sender_tool_call_id:
            raise ValueError("sender Operation、Step 和 ToolCall 身份不能为空")
        if not target_child_session_id:
            raise ValueError("child_session_id 不能为空")
        return self._store.prepare_interrupt_agent(
            sender_operation_id=sender_operation_id,
            sender_step_id=sender_step_id,
            sender_tool_call_id=sender_tool_call_id,
            target_child_session_id=target_child_session_id,
            handled_at=self._now(),
        )

    def prepare_cancel_delegation(
        self,
        sender_operation_id: str,
        sender_step_id: str,
        sender_tool_call_id: str,
        target_child_session_id: str,
    ) -> str | None:
        """旧 Package 迁移兼容入口；新流程使用 interrupt_agent。"""
        return self._store.prepare_cancel_delegation(
            sender_operation_id=sender_operation_id,
            sender_step_id=sender_step_id,
            sender_tool_call_id=sender_tool_call_id,
            target_child_session_id=target_child_session_id,
            handled_at=self._now(),
        )

    def send_child_report(
        self,
        sender_operation_id: str,
        sender_step_id: str,
        sender_tool_call_id: str,
        output: str,
    ) -> InboxMessage:
        """把 child 的自包含报告以 steer 投递给唯一 direct parent。"""
        if not sender_operation_id or not sender_step_id or not sender_tool_call_id:
            raise ValueError("sender Operation、Step 和 ToolCall 身份不能为空")
        if not isinstance(output, str) or not output.strip():
            raise ValueError("report output 不能为空")
        operation = self._store.load_operation(sender_operation_id)
        if operation is None:
            raise StorageIntegrityError("sender Operation 不存在")
        delegation = self._store.load_delegation(operation.session_id)
        if delegation is None:
            raise StorageConflictError("只有 delegated child 才能 report")
        parent_operation = self._store.load_operation(delegation.parent_operation_id)
        if parent_operation is None:
            raise StorageIntegrityError("report 的 parent Operation 不存在")
        parent_session = self._store.load_session(parent_operation.session_id)
        if parent_session is None:
            raise StorageIntegrityError("report 的 parent Session 不存在")
        message_id = _agent_message_id(
            sender_operation_id, sender_step_id, sender_tool_call_id
        )
        message = UserMessage(
            (
                TextBlock(
                    f"Background subagent {operation.session_id} reported:\n{output}"
                ),
            )
        )
        source = AgentMessageSource(
            sender_session_id=operation.session_id,
            sender_operation_id=sender_operation_id,
            form="steer",
        )
        return self._store.send_child_report(
            sender_operation_id=sender_operation_id,
            sender_step_id=sender_step_id,
            sender_tool_call_id=sender_tool_call_id,
            parent_session_id=parent_session.session_id,
            message_id=message_id,
            message=message,
            source=source,
            created_at=self._now(),
        )

    def list_child_agents(
        self,
        sender_operation_id: str,
        sender_step_id: str,
        sender_tool_call_id: str,
    ) -> tuple["ChildAgentSnapshot", ...]:
        """读取当前 sender Session 的 direct child 快照。"""
        operation = self._store.load_operation(sender_operation_id)
        state = self._store.load_run_state(sender_operation_id)
        if operation is None or state is None:
            raise StorageIntegrityError("sender Operation 或 AgentRunState 不存在")
        sender_session = self._store.load_session(operation.session_id)
        if sender_session is None:
            raise StorageIntegrityError("sender Session 不存在")
        if sender_session.active_operation_id != sender_operation_id:
            raise StorageConflictError(
                "sender Operation 不是 Session 的 active Operation"
            )
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
        if (
            state.status != "running"
            or step is None
            or step.step_id != sender_step_id
            or step.phase != "awaiting_tools"
            or call is None
            or call.tool_name != "list_agents"
            or call.status != "intent_recorded"
            or call.execution_intent is not None
            or call.arguments != {}
        ):
            raise StorageConflictError(
                "sender ToolCall 不是当前 list_agents intent_recorded"
            )

        snapshots: list[tuple[datetime, str, ChildAgentSnapshot]] = []
        for parent_operation in self._store.list_operations(
            session_id=operation.session_id
        ):
            for delegation in self._store.list_delegations(
                parent_operation_id=parent_operation.operation_id
            ):
                child = self._store.load_session(delegation.child_session_id)
                if child is None:
                    raise StorageIntegrityError("delegation child Session 不存在")
                snapshots.append(
                    (
                        delegation.created_at,
                        delegation.child_session_id,
                        self._snapshot_child(child),
                    )
                )
        snapshots.sort(key=lambda item: (item[0], item[1]))
        return tuple(item[2] for item in snapshots)

    def inspect_wait_target(
        self,
        sender_operation_id: str,
        sender_step_id: str,
        sender_tool_call_id: str,
        target_child_session_id: str,
        timeout_seconds: float,
    ) -> "ChildAgentSnapshot":
        """校验 wait_delegation ToolCall 并返回 direct child 快照。"""
        operation = self._store.load_operation(sender_operation_id)
        state = self._store.load_run_state(sender_operation_id)
        if operation is None or state is None:
            raise StorageIntegrityError("sender Operation 或 AgentRunState 不存在")
        session = self._store.load_session(operation.session_id)
        if session is None or session.active_operation_id != sender_operation_id:
            raise StorageConflictError(
                "sender Operation 不是 Session 的 active Operation"
            )
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
        expected_arguments = {
            "child_session_id": target_child_session_id,
            "timeout_seconds": timeout_seconds,
        }
        if (
            state.status != "running"
            or step is None
            or step.step_id != sender_step_id
            or step.phase != "awaiting_tools"
            or call is None
            or call.tool_name != "wait_delegation"
            or call.status != "intent_recorded"
            or call.execution_intent is not None
            or dict(call.arguments) != expected_arguments
        ):
            raise StorageConflictError(
                "sender ToolCall 不是当前 wait_delegation intent_recorded"
            )
        direct_children = {
            delegation.child_session_id
            for parent in self._store.list_operations(session_id=operation.session_id)
            for delegation in self._store.list_delegations(
                parent_operation_id=parent.operation_id
            )
        }
        if target_child_session_id not in direct_children:
            raise StorageConflictError("wait_delegation 目标不是 direct child")
        child = self._store.load_session(target_child_session_id)
        if child is None:
            raise StorageIntegrityError("delegation child Session 不存在")
        return self._snapshot_child(child)

    def load_final_assistant(
        self, snapshot: "ChildAgentSnapshot"
    ) -> AssistantMessage | None:
        node_id = snapshot.final_assistant_node_id
        if node_id is None:
            return None
        node = self._store.load_node(node_id)
        if (
            node is None
            or node.session_id != snapshot.child_session_id
            or not isinstance(node.content, AssistantMessage)
        ):
            raise StorageIntegrityError("child final_assistant_node_id 指向无效")
        return node.content

    def _snapshot_child(self, session: ConversationSession) -> "ChildAgentSnapshot":
        operations = self._store.list_operations(session_id=session.session_id)
        latest = operations[-1] if operations else None
        if session.archived_at is not None:
            return self._snapshot_terminal_or_archived(
                session, latest, status="archived"
            )
        if session.active_operation_id is not None:
            operation = self._store.load_operation(session.active_operation_id)
            state = self._store.load_run_state(session.active_operation_id)
            if (
                operation is None
                or state is None
                or operation.session_id != session.session_id
            ):
                raise StorageIntegrityError("child Session 的 active Operation 不完整")
            return self._snapshot_from_state(session, operation, state)
        pending = self._store.list_pending(session_id=session.session_id)
        if any(item.delivery in {"followup", "steer"} for item in pending):
            return ChildAgentSnapshot(
                child_session_id=session.session_id,
                agent_id=session.agent_id,
                status="ready",
                operation_id=None,
                waiting_reason=None,
                completed_step_count=0,
                final_assistant_node_id=None,
                error=None,
                updated_at=session.updated_at,
                phase="pending_inbox",
                request_attempt=0,
                pending_message_count=sum(
                    item.delivery in {"followup", "steer"} for item in pending
                ),
            )
        if latest is None:
            return ChildAgentSnapshot(
                child_session_id=session.session_id,
                agent_id=session.agent_id,
                status="idle",
                operation_id=None,
                waiting_reason=None,
                completed_step_count=0,
                final_assistant_node_id=None,
                error=None,
                updated_at=session.updated_at,
            )
        state = self._store.load_run_state(latest.operation_id)
        if state is None or state.status not in {"succeeded", "failed", "cancelled"}:
            raise StorageIntegrityError("child Session 最新 Operation 缺少终态")
        return self._snapshot_from_state(session, latest, state)

    def _snapshot_terminal_or_archived(
        self,
        session: ConversationSession,
        operation: SessionOperation | None,
        *,
        status: "ChildAgentStatus",
    ) -> "ChildAgentSnapshot":
        if operation is None:
            return ChildAgentSnapshot(
                child_session_id=session.session_id,
                agent_id=session.agent_id,
                status=status,
                operation_id=None,
                waiting_reason=None,
                completed_step_count=0,
                final_assistant_node_id=None,
                error=None,
                updated_at=session.updated_at,
            )
        state = self._store.load_run_state(operation.operation_id)
        if state is None or state.status not in {"succeeded", "failed", "cancelled"}:
            raise StorageIntegrityError("已归档 child Session 缺少终态 Operation")
        return self._snapshot_from_state(
            session,
            operation,
            state,
            status=status,
        )

    def _snapshot_from_state(
        self,
        session: ConversationSession,
        operation: SessionOperation,
        state: AgentRunState,
        *,
        status: "ChildAgentStatus | None" = None,
    ) -> "ChildAgentSnapshot":
        return ChildAgentSnapshot(
            child_session_id=session.session_id,
            agent_id=session.agent_id,
            status=status or state.status,
            operation_id=operation.operation_id,
            waiting_reason=state.waiting_reason,
            completed_step_count=state.completed_step_count,
            final_assistant_node_id=state.final_assistant_node_id,
            error=state.error,
            updated_at=session.updated_at,
            phase=(
                state.current_step.phase if state.current_step is not None else None
            ),
            request_attempt=(
                state.current_step.request_attempt
                if state.current_step is not None
                else 0
            ),
            pending_message_count=len(
                self._store.list_pending(session_id=session.session_id)
            ),
        )


ChildAgentStatus = AgentRunStatus | Literal["ready", "idle", "archived"]


@dataclass(frozen=True)
class ChildAgentSnapshot:
    child_session_id: str
    agent_id: str
    status: ChildAgentStatus
    operation_id: str | None
    waiting_reason: WaitingReason | None
    completed_step_count: int
    final_assistant_node_id: str | None
    error: AgentRunError | None
    updated_at: datetime | None = None
    phase: str | None = None
    request_attempt: int = 0
    pending_message_count: int = 0

    def to_dict(self) -> dict[str, object]:
        return {
            "child_session_id": self.child_session_id,
            "agent_id": self.agent_id,
            "status": self.status,
            "operation_id": self.operation_id,
            "waiting_reason": self.waiting_reason,
            "completed_step_count": self.completed_step_count,
            "final_assistant_node_id": self.final_assistant_node_id,
            "error": (
                {
                    "code": self.error.code,
                    "message": self.error.message,
                    "retryable": self.error.retryable,
                }
                if self.error is not None
                else None
            ),
            "updated_at": (
                self.updated_at.isoformat() if self.updated_at is not None else None
            ),
            "phase": self.phase,
            "request_attempt": self.request_attempt,
            "pending_message_count": self.pending_message_count,
        }


def _followup_message_id(
    sender_operation_id: str, sender_step_id: str, sender_tool_call_id: str
) -> str:
    return _agent_message_id(
        sender_operation_id,
        sender_step_id,
        sender_tool_call_id,
    )


def _agent_message_id(
    sender_operation_id: str, sender_step_id: str, sender_tool_call_id: str
) -> str:
    payload = json.dumps(
        {
            "sender_operation_id": sender_operation_id,
            "sender_step_id": sender_step_id,
            "sender_tool_call_id": sender_tool_call_id,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return "message_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()
