"""Delegation 的 durable acceptance 事务。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
import hashlib
import json
from typing import Protocol
from uuid import uuid4

from pickel.agents.agent_package import AgentPackageVersion
from pickel.conversations.agent_message import UserMessage
from pickel.inbox.message import AgentMessageSource, InboxMessage
from pickel.conversations.conversation_session import ConversationSession
from pickel.operations.agent_delegation import AgentDelegation
from pickel.operations.agent_run_state import AgentRunState, DelegateAgentIntent
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


def _followup_message_id(
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
