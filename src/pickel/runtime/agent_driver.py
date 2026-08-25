"""AgentDriver：判断 Session 是否可运行，并串行接受或恢复 Operation。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from pickel.conversations.conversation_session import ConversationSession
from pickel.conversations.conversation_store import ConversationStore
from pickel.inbox.store import InboxStore
from pickel.operations.operation_service import AcceptedOperation, OperationService
from pickel.runtime.agent_inbox import AgentInbox
from pickel.runtime.operation_driver import OperationDriveResult, OperationDriver
from pickel.workspaces.workspace_binding import WorkspaceBinding


class PackageVersionResolver(Protocol):
    def __call__(
        self, *, session: ConversationSession
    ) -> tuple[str, WorkspaceBinding]: ...


class CancelOperation(Protocol):
    def __call__(self, operation_id: str, *, reason: str) -> bool: ...


@dataclass(frozen=True)
class AgentDriveResult:
    """一次 wake 的结果；无待办时 operation_result 为 None。"""

    operation_result: OperationDriveResult | None
    accepted: AcceptedOperation | None = None


class AgentDriver:
    """Session 级调度入口；不执行 Provider、Tool 或状态转换。"""

    def __init__(
        self,
        *,
        conversation_store: ConversationStore,
        inbox_store: InboxStore,
        operation_service: OperationService,
        operation_driver: OperationDriver,
        package_resolver: PackageVersionResolver,
        cancel_operation: CancelOperation | None = None,
        wake_callback: Callable[[str], None] | None = None,
    ) -> None:
        self._conversations = conversation_store
        self._inbox_store = inbox_store
        self._operations = operation_service
        self._operation_driver = operation_driver
        self._package_resolver = package_resolver
        self._cancel_operation = cancel_operation
        self._wake_callback = wake_callback

    async def drive_once(
        self,
        *,
        session_id: str,
        consume_delta=None,
        host_calls=None,
    ) -> AgentDriveResult:
        session = self._load_session(session_id)
        if session.archived_at is not None:
            return AgentDriveResult(None)

        if session.active_operation_id is not None:
            result = await self._operation_driver.drive_operation(
                session.active_operation_id,
                consume_delta=consume_delta,
                host_calls=host_calls,
            )
            return AgentDriveResult(result)

        pending = self._inbox_store.list_pending(session_id=session_id)
        if not pending:
            return AgentDriveResult(None)
        package_version_id, workspace_binding = self._package_resolver(session=session)
        accepted = self._operations.accept_pending_message(
            message=pending[0],
            agent_package_version_id=package_version_id,
            workspace_binding=workspace_binding,
            expected_node_id=session.active_node_id,
        )
        if accepted is None:
            # 竞争失败不重放副作用；下一次 wake 会重新读取事实。
            return AgentDriveResult(None)
        result = await self._operation_driver.drive_operation(
            accepted.operation.operation_id,
            consume_delta=consume_delta,
            host_calls=host_calls,
        )
        return AgentDriveResult(result, accepted)

    async def when_idle(
        self, *, session_id: str, consume_delta=None, host_calls=None
    ) -> AgentDriveResult:
        """推进一次，直到 OperationDriver 返回等待点或终态。"""
        return await self.drive_once(
            session_id=session_id,
            consume_delta=consume_delta,
            host_calls=host_calls,
        )

    def cancel(self, *, session_id: str, reason: str) -> bool:
        session = self._load_session(session_id)
        if session.active_operation_id is None:
            return True
        if self._cancel_operation is None:
            raise RuntimeError("AgentDriver 未配置 CancelOperation")
        return self._cancel_operation(session.active_operation_id, reason=reason)

    def wake(self, session_id: str) -> None:
        if self._wake_callback is not None:
            self._wake_callback(session_id)

    def _load_session(self, session_id: str) -> ConversationSession:
        session = self._conversations.load_session(session_id)
        if session is None:
            raise LookupError(f"ConversationSession 不存在: {session_id}")
        return session


def build_agent_inbox(*, session_id: str, store: InboxStore) -> AgentInbox:
    """为 UI/Host 创建只绑定一个 Session 的 Inbox 投影。"""
    return AgentInbox(session_id=session_id, store=store)
