"""v10 Runtime 领域实体的进程内存储适配器。

该实现故意只保存 v10 实体；锁就是内存适配器的事务边界。所有涉及多个
实体的操作先在锁内完成完整前置条件校验，再一次性更新字典，因此失败时
不会留下半个 Operation 或半条 InboxMessage。
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from threading import RLock

from pickel.agents.agent_package import (
    AgentPackageVersion,
    decode_agent_package_content,
    package_version_id_for_content,
)
from pickel.artifacts.artifact import Artifact
from pickel.conversations.agent_message import AgentMessage, UserMessage
from pickel.conversations.content_blocks import ArtifactBlock, TextBlock
from pickel.conversations.conversation_node import ConversationNode, HistoryCompaction
from pickel.conversations.conversation_session import ConversationSession
from pickel.inbox.message import (
    AgentMessageSource,
    InboxMessage,
    MessageDelivery,
    MessageSource,
)
from pickel.operations.agent_delegation import AgentDelegation
from pickel.operations.agent_run_state import AgentRunState, DelegateAgentIntent
from pickel.operations.session_operation import SessionOperation
from pickel.persistence.errors import StorageConflictError, StorageIntegrityError
from pickel.workspaces.workspace import Workspace


class InMemoryRuntimeStore:
    """与 SQLite v10 遵循同一实体、CAS 和原子操作合同。"""

    def __init__(self) -> None:
        self._workspaces: dict[str, Workspace] = {}
        self._sessions: dict[str, ConversationSession] = {}
        self._nodes: dict[str, ConversationNode] = {}
        self._inbox: dict[str, InboxMessage] = {}
        self._packages: dict[str, AgentPackageVersion] = {}
        self._operations: dict[str, SessionOperation] = {}
        self._run_states: dict[str, AgentRunState] = {}
        self._delegations: dict[str, AgentDelegation] = {}
        self._artifacts: dict[str, Artifact] = {}
        self._lock = RLock()

    # Workspace ---------------------------------------------------------
    def create_session(
        self, *, workspace: Workspace, session: ConversationSession
    ) -> None:
        with self._lock:
            if not workspace.root_path.is_dir():
                raise StorageIntegrityError(
                    f"Workspace root_path 必须是现有目录: {workspace.root_path}"
                )
            if session.workspace_id != workspace.workspace_id:
                raise StorageIntegrityError("Session.workspace_id 与 Workspace 不匹配")
            if any(
                value is not None
                for value in (
                    session.active_node_id,
                    session.active_operation_id,
                    session.title,
                    session.title_source,
                    session.archived_at,
                )
            ):
                raise StorageIntegrityError("新 Session 必须是空的未归档 Session")
            existing_session = self._sessions.get(session.session_id)
            existing_workspace = self._workspaces.get(workspace.workspace_id)
            if existing_session is not None:
                if (
                    existing_session == session
                    and existing_workspace is not None
                    and existing_workspace.root_path == workspace.root_path
                ):
                    return
                raise StorageIntegrityError(
                    f"ConversationSession 已存在: {session.session_id}"
                )
            if (
                existing_workspace is not None
                and existing_workspace.root_path != workspace.root_path
            ):
                raise StorageIntegrityError(
                    f"Workspace ID 已存在但内容不同: {workspace.workspace_id}"
                )
            by_root = next(
                (
                    item
                    for item in self._workspaces.values()
                    if item.root_path == workspace.root_path
                ),
                None,
            )
            if by_root is not None and by_root.workspace_id != workspace.workspace_id:
                raise StorageIntegrityError(
                    f"Workspace root_path 已存在: {workspace.root_path}"
                )
            if existing_workspace is None:
                self._workspaces[workspace.workspace_id] = workspace
            self._sessions[session.session_id] = session

    def load_workspace(self, workspace_id: str) -> Workspace | None:
        with self._lock:
            return self._workspaces.get(workspace_id)

    def find_workspace_by_root(self, root_path: str | Path) -> Workspace | None:
        root = Path(root_path).expanduser().resolve(strict=False)
        with self._lock:
            return next(
                (item for item in self._workspaces.values() if item.root_path == root),
                None,
            )

    # Conversation ------------------------------------------------------
    def load_session(self, session_id: str) -> ConversationSession | None:
        with self._lock:
            return self._sessions.get(session_id)

    def list_sessions(
        self, *, limit: int = 20, cwd: str | None = None
    ) -> tuple[ConversationSession, ...]:
        if limit <= 0:
            return ()
        normalized = Path(cwd).expanduser().resolve(strict=False) if cwd else None
        with self._lock:
            values = [
                session
                for session in self._sessions.values()
                if normalized is None or session.cwd == normalized
            ]
            values.sort(
                key=lambda item: (-item.updated_at.timestamp(), item.session_id)
            )
            return tuple(values[:limit])

    def list_runnable_session_ids(self) -> tuple[str, ...]:
        """返回启动恢复候选，不受历史查询的默认分页限制。"""
        with self._lock:
            result: list[str] = []
            for session in self._sessions.values():
                if session.archived_at is not None:
                    continue
                if session.active_operation_id is not None:
                    state = self._run_states.get(session.active_operation_id)
                    if state is not None and state.status in {
                        "queued",
                        "running",
                        "cancelling",
                    }:
                        result.append(session.session_id)
                    continue
                if any(
                    item.session_id == session.session_id
                    and item.status == "pending"
                    and item.delivery in {"followup", "steer"}
                    for item in self._inbox.values()
                ):
                    result.append(session.session_id)
            return tuple(sorted(result))

    def append_node(
        self, *, node: ConversationNode, expected_node_id: str | None
    ) -> bool:
        with self._lock:
            session = self._require_session_unlocked(node.session_id)
            if (
                session.archived_at is not None
                or session.active_operation_id is not None
                or session.active_node_id != expected_node_id
                or node.parent_node_id != expected_node_id
            ):
                return False
            self._validate_node_unlocked(node)
            self._validate_content_artifacts_unlocked(node.content)
            existing = self._nodes.get(node.node_id)
            if existing is not None:
                if existing == node:
                    return False
                raise StorageIntegrityError(
                    f"ConversationNode ID 已存在: {node.node_id}"
                )
            self._nodes[node.node_id] = node
            self._sessions[node.session_id] = replace(
                session, active_node_id=node.node_id, updated_at=node.created_at
            )
            return True

    def load_node(self, node_id: str) -> ConversationNode | None:
        with self._lock:
            return self._nodes.get(node_id)

    def list_branch_nodes(
        self, session_id: str, leaf_node_id: str | None
    ) -> tuple[ConversationNode, ...]:
        with self._lock:
            self._require_session_unlocked(session_id)
            if leaf_node_id is None:
                return ()
            leaf = leaf_node_id
            result: list[ConversationNode] = []
            seen: set[str] = set()
            while leaf is not None:
                if leaf in seen:
                    raise StorageIntegrityError("ConversationNode parent 链存在环")
                seen.add(leaf)
                node = self._nodes.get(leaf)
                if node is None or node.session_id != session_id:
                    raise StorageIntegrityError(
                        f"ConversationNode 不存在或 Session 不匹配: {leaf}"
                    )
                result.append(node)
                leaf = node.parent_node_id
            result.reverse()
            return tuple(result)

    def move_active_node(
        self,
        *,
        session_id: str,
        expected_node_id: str | None,
        new_node_id: str | None,
        updated_at: datetime,
    ) -> bool:
        with self._lock:
            session = self._require_session_unlocked(session_id)
            if (
                session.archived_at is not None
                or session.active_operation_id is not None
            ):
                return False
            if session.active_node_id != expected_node_id:
                return False
            if new_node_id is not None:
                node = self._nodes.get(new_node_id)
                if node is None or node.session_id != session_id:
                    raise StorageIntegrityError("new_node_id 不属于 Session")
            self._sessions[session_id] = replace(
                session, active_node_id=new_node_id, updated_at=updated_at
            )
            return True

    def archive_session(self, *, session_id: str, archived_at: datetime) -> None:
        with self._lock:
            session = self._require_session_unlocked(session_id)
            if session.archived_at is not None:
                return
            self._assert_idle_unlocked(session_id)
            if any(
                item.session_id == session_id and item.status == "pending"
                for item in self._inbox.values()
            ):
                raise StorageIntegrityError(
                    "存在 pending InboxMessage，不能归档 Session"
                )
            self._sessions[session_id] = replace(
                session, archived_at=archived_at, updated_at=archived_at
            )

    def unarchive_session(self, *, session_id: str, updated_at: datetime) -> None:
        with self._lock:
            session = self._require_session_unlocked(session_id)
            if session.archived_at is None:
                return
            self._sessions[session_id] = replace(
                session, archived_at=None, updated_at=updated_at
            )

    def delete_session(self, *, session_id: str) -> None:
        with self._lock:
            self._assert_deletable_unlocked(session_id)
            if any(
                item.child_session_id == session_id
                for item in self._delegations.values()
            ):
                raise StorageIntegrityError(
                    "存在 AgentDelegation，不能单独删除 Session"
                )
            if any(
                self._operation_has_session_unlocked(item, session_id)
                for item in self._delegations.values()
            ):
                raise StorageIntegrityError("Session 是父 Operation，不能单独删除")
            self._delete_sessions_unlocked({session_id})

    def delete_session_tree(self, *, session_id: str) -> None:
        with self._lock:
            self._require_session_unlocked(session_id)
            targets = self._descendant_sessions_unlocked(session_id)
            parent = next(
                (
                    item
                    for item in self._delegations.values()
                    if item.child_session_id == session_id
                ),
                None,
            )
            if parent is not None and parent.parent_operation_id in self._operations:
                parent_operation = self._operations[parent.parent_operation_id]
                if parent_operation.session_id not in targets:
                    raise StorageIntegrityError(
                        "根 Session 有子树外部 parent Delegation"
                    )
            for target in targets:
                self._assert_deletable_unlocked(target)
            self._delete_sessions_unlocked(targets)

    # Inbox -------------------------------------------------------------
    def send_message(
        self,
        *,
        message_id: str,
        session_id: str,
        delivery: MessageDelivery,
        message: UserMessage,
        source: MessageSource,
        created_at: datetime,
    ) -> InboxMessage:
        with self._lock:
            session = self._require_session_unlocked(session_id)
            if session.archived_at is not None:
                raise StorageIntegrityError("归档 Session 不能接收 InboxMessage")
            if message_id in self._inbox:
                raise StorageIntegrityError(f"InboxMessage ID 已存在: {message_id}")
            self._validate_content_artifacts_unlocked(message)
            sequence = (
                max(
                    (
                        item.sequence
                        for item in self._inbox.values()
                        if item.session_id == session_id
                    ),
                    default=0,
                )
                + 1
            )
            stored = InboxMessage(
                message_id=message_id,
                session_id=session_id,
                sequence=sequence,
                delivery=delivery,
                message=message,
                source=source,
                created_at=created_at,
            )
            self._inbox[message_id] = stored
            return stored

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
    ) -> InboxMessage:
        with self._lock:
            operation = self._operations.get(sender_operation_id)
            if operation is None:
                raise StorageIntegrityError("sender Operation 不存在")
            sender = self._sessions.get(operation.session_id)
            if sender is None:
                raise StorageIntegrityError("sender Session 不存在")
            delegation = self._delegations.get(target_child_session_id)
            target = self._sessions.get(target_child_session_id)
            if delegation is None:
                raise StorageConflictError("target 不是 sender Session 的 direct child")
            parent_operation = self._operations.get(delegation.parent_operation_id)
            if parent_operation is None:
                raise StorageIntegrityError("delegation parent Operation 不存在")
            if parent_operation.session_id != operation.session_id:
                raise StorageConflictError("target 不是 sender Session 的 direct child")
            if target is None:
                raise StorageIntegrityError("target child Session 不存在")
            if source != AgentMessageSource(
                sender_session_id=operation.session_id,
                sender_operation_id=sender_operation_id,
                form="followup",
            ):
                raise StorageIntegrityError("send_message source 不匹配")
            existing = self._inbox.get(message_id)
            if existing is not None:
                if (
                    existing.session_id != target_child_session_id
                    or existing.delivery != "followup"
                    or existing.message != message
                    or existing.source != source
                ):
                    raise StorageConflictError("send_message 的稳定 ID 语义冲突")
                return existing
            state = self._run_states.get(sender_operation_id)
            if state is None:
                raise StorageIntegrityError("sender AgentRunState 不存在")
            if sender.active_operation_id != sender_operation_id:
                raise StorageConflictError("sender Session 未指向 sender Operation")
            if state.status != "running":
                raise StorageConflictError("sender Operation 必须处于 running")
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
            expected_text = call.arguments.get("message") if call else None
            if (
                step is None
                or step.step_id != sender_step_id
                or step.phase != "awaiting_tools"
                or call is None
                or call.tool_name != "send_message"
                or call.status != "intent_recorded"
                or call.execution_intent is not None
                or call.arguments.get("child_session_id") != target_child_session_id
                or not isinstance(expected_text, str)
                or message != UserMessage((TextBlock(expected_text),))
            ):
                raise StorageConflictError(
                    "sender ToolCall 不是当前 send_message intent_recorded"
                )
            if target.archived_at is not None:
                raise StorageConflictError("归档 child Session 不能接收新的消息")
            self._validate_content_artifacts_unlocked(message)
            sequence = (
                max(
                    (
                        item.sequence
                        for item in self._inbox.values()
                        if item.session_id == target_child_session_id
                    ),
                    default=0,
                )
                + 1
            )
            stored = InboxMessage(
                message_id=message_id,
                session_id=target_child_session_id,
                sequence=sequence,
                delivery="followup",
                message=message,
                source=source,
                created_at=created_at,
            )
            self._inbox[message_id] = stored
            return stored

    def prepare_interrupt_agent(
        self,
        *,
        sender_operation_id: str,
        sender_step_id: str,
        sender_tool_call_id: str,
        target_child_session_id: str,
        handled_at: datetime,
        _expected_tool_name: str = "interrupt_agent",
    ) -> str | None:
        """原子校验 interrupt_agent，并返回 child 当时的 active Operation。

        中断不丢弃目标 child Inbox；目标 Operation 的普通 cancellation
        reconciliation 只处理它自己创建的后代消息。
        """
        with self._lock:
            operation = self._operations.get(sender_operation_id)
            if operation is None:
                raise StorageIntegrityError("sender Operation 不存在")
            sender = self._sessions.get(operation.session_id)
            target = self._sessions.get(target_child_session_id)
            delegation = self._delegations.get(target_child_session_id)
            if sender is None:
                raise StorageIntegrityError("sender Session 不存在")
            if target is None:
                raise StorageConflictError("target child Session 不存在")
            parent_operation = (
                self._operations.get(delegation.parent_operation_id)
                if delegation is not None
                else None
            )
            if (
                delegation is None
                or parent_operation is None
                or parent_operation.session_id != operation.session_id
            ):
                raise StorageConflictError("target 不是 sender Session 的 direct child")
            if sender.active_operation_id != sender_operation_id:
                raise StorageConflictError(
                    "sender Operation 不是 Session 的 active Operation"
                )
            state = self._run_states.get(sender_operation_id)
            step = state.current_step if state is not None else None
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
                state is None
                or state.status != "running"
                or step is None
                or step.step_id != sender_step_id
                or step.phase != "awaiting_tools"
                or call is None
                or call.tool_name != _expected_tool_name
                or call.status != "intent_recorded"
                or call.execution_intent is not None
                or call.arguments != {"child_session_id": target_child_session_id}
            ):
                raise StorageConflictError(
                    f"sender ToolCall 不是当前 {_expected_tool_name} intent_recorded"
                )
            return target.active_operation_id

    def prepare_cancel_delegation(
        self,
        *,
        sender_operation_id: str,
        sender_step_id: str,
        sender_tool_call_id: str,
        target_child_session_id: str,
        handled_at: datetime,
    ) -> str | None:
        """旧 Package 迁移兼容入口；新 Package 使用 interrupt_agent。"""
        operation_id = self.prepare_interrupt_agent(
            sender_operation_id=sender_operation_id,
            sender_step_id=sender_step_id,
            sender_tool_call_id=sender_tool_call_id,
            target_child_session_id=target_child_session_id,
            handled_at=handled_at,
            _expected_tool_name="cancel_delegation",
        )
        # 仅为历史 Package 保留旧行为。新 interrupt_agent 明确保留目标 Inbox。
        with self._lock:
            operation = self._operations.get(sender_operation_id)
            for message in tuple(self._inbox.values()):
                source = message.source
                source_operation = (
                    self._operations.get(source.sender_operation_id)
                    if isinstance(source, AgentMessageSource)
                    else None
                )
                if (
                    message.session_id == target_child_session_id
                    and message.status == "pending"
                    and isinstance(source, AgentMessageSource)
                    and operation is not None
                    and source.sender_session_id == operation.session_id
                    and source.form == message.delivery
                    and source_operation is not None
                    and source_operation.session_id == operation.session_id
                ):
                    self._inbox[message.message_id] = replace(
                        message,
                        status="discarded",
                        outcome_reason="direct child 已被取消",
                        handled_at=handled_at,
                    )
        return operation_id

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
    ) -> InboxMessage:
        """原子校验 child 直系父关系并追加 steer report。"""
        with self._lock:
            operation = self._operations.get(sender_operation_id)
            if operation is None:
                raise StorageIntegrityError("sender Operation 不存在")
            sender = self._sessions.get(operation.session_id)
            if sender is None:
                raise StorageIntegrityError("sender Session 不存在")
            delegation = self._delegations.get(operation.session_id)
            if delegation is None:
                raise StorageConflictError("只有 delegated child 才能 report")
            parent_operation = self._operations.get(delegation.parent_operation_id)
            parent = (
                self._sessions.get(parent_operation.session_id)
                if parent_operation is not None
                else None
            )
            if parent_operation is None or parent is None:
                raise StorageIntegrityError("report 的 parent Operation/Session 不存在")
            if parent.session_id != parent_session_id:
                raise StorageIntegrityError("report parent Session 路由不匹配")
            expected_source = AgentMessageSource(
                sender_session_id=operation.session_id,
                sender_operation_id=sender_operation_id,
                form="steer",
            )
            if source != expected_source:
                raise StorageIntegrityError("report source 不匹配")
            existing = self._inbox.get(message_id)
            if existing is not None:
                if (
                    existing.session_id != parent_session_id
                    or existing.delivery != "steer"
                    or existing.message != message
                    or existing.source != source
                ):
                    raise StorageConflictError("report 的稳定 ID 语义冲突")
                return existing
            if sender.active_operation_id != sender_operation_id:
                raise StorageConflictError("sender Session 未指向 sender Operation")
            state = self._run_states.get(sender_operation_id)
            if state is None:
                raise StorageIntegrityError("sender AgentRunState 不存在")
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
            expected_output = call.arguments.get("output") if call else None
            expected_message = (
                UserMessage(
                    (
                        TextBlock(
                            f"Background subagent {operation.session_id} reported:\n"
                            f"{expected_output}"
                        ),
                    )
                )
                if isinstance(expected_output, str)
                else None
            )
            if (
                step is None
                or step.step_id != sender_step_id
                or step.phase != "awaiting_tools"
                or state.status != "running"
                or call is None
                or call.tool_name != "report"
                or call.status != "intent_recorded"
                or call.execution_intent is not None
                or call.arguments != {"output": expected_output}
                or expected_message != message
            ):
                raise StorageConflictError(
                    "sender ToolCall 不是当前 report intent_recorded"
                )
            if parent.archived_at is not None:
                raise StorageConflictError("归档 parent Session 不能接收新的 report")
            self._validate_content_artifacts_unlocked(message)
            sequence = (
                max(
                    (
                        item.sequence
                        for item in self._inbox.values()
                        if item.session_id == parent_session_id
                    ),
                    default=0,
                )
                + 1
            )
            stored = InboxMessage(
                message_id=message_id,
                session_id=parent_session_id,
                sequence=sequence,
                delivery="steer",
                message=message,
                source=source,
                created_at=created_at,
            )
            self._inbox[message_id] = stored
            return stored

    def list_pending(
        self, *, session_id: str, delivery: str | None = None
    ) -> tuple[InboxMessage, ...]:
        with self._lock:
            values = [
                item
                for item in self._inbox.values()
                if item.session_id == session_id
                and item.status == "pending"
                and (delivery is None or item.delivery == delivery)
            ]
            return tuple(
                sorted(values, key=lambda item: (item.sequence, item.message_id))
            )

    def list_pending_step_messages(
        self, *, session_id: str
    ) -> tuple[InboxMessage, ...]:
        with self._lock:
            values = [
                item
                for item in self._inbox.values()
                if item.session_id == session_id
                and item.status == "pending"
                and item.delivery in {"steer", "inject"}
            ]
            return tuple(
                sorted(values, key=lambda item: (item.sequence, item.message_id))
            )

    def load_message(self, message_id: str) -> InboxMessage | None:
        with self._lock:
            return self._inbox.get(message_id)

    def claim_message(
        self,
        *,
        message_id: str,
        operation_id: str,
        step_id: str | None,
        handled_at: datetime,
    ) -> bool:
        with self._lock:
            message = self._inbox.get(message_id)
            if message is None or message.status != "pending":
                return False
            if operation_id not in self._operations:
                raise StorageIntegrityError(f"SessionOperation 不存在: {operation_id}")
            if self._operations[operation_id].session_id != message.session_id:
                raise StorageIntegrityError(
                    "InboxMessage 与 Operation 不属于同一 Session"
                )
            self._inbox[message_id] = replace(
                message,
                status="claimed",
                claimed_operation_id=operation_id,
                claimed_step_id=step_id,
                handled_at=handled_at,
            )
            return True

    def discard_message(
        self, *, message_id: str, reason: str, handled_at: datetime
    ) -> bool:
        with self._lock:
            message = self._inbox.get(message_id)
            if message is None or message.status != "pending":
                return False
            if not reason:
                raise ValueError("discard reason 不能为空")
            self._inbox[message_id] = replace(
                message,
                status="discarded",
                outcome_reason=reason,
                handled_at=handled_at,
            )
            return True

    def discard_cancellation_messages(
        self, *, root_operation_id: str, reason: str, handled_at: datetime
    ) -> tuple[str, ...]:
        """丢弃取消祖先发往真实后代的 pending AgentMessage。"""
        if not reason:
            raise ValueError("取消消息丢弃原因不能为空")
        with self._lock:
            graph = self._cancellation_graph_unlocked(root_operation_id)
            discarded: list[str] = []
            for message in tuple(self._inbox.values()):
                if message.status != "pending":
                    continue
                if not self._is_cancellation_message_unlocked(message, graph):
                    continue
                self._inbox[message.message_id] = replace(
                    message,
                    status="discarded",
                    outcome_reason=reason,
                    handled_at=handled_at,
                )
                discarded.append(message.message_id)
            return tuple(sorted(discarded))

    def cancellation_ready(self, *, root_operation_id: str) -> bool:
        with self._lock:
            graph = self._cancellation_graph_unlocked(root_operation_id)
            for operation_id in graph["operation_ids"] - {root_operation_id}:
                state = self._run_states.get(operation_id)
                if state is None or state.status not in {
                    "succeeded",
                    "failed",
                    "cancelled",
                }:
                    return False
            return not any(
                message.status == "pending"
                and self._is_cancellation_message_unlocked(message, graph)
                for message in self._inbox.values()
            )

    # Package and Artifact ---------------------------------------------
    def insert_agent_package_version(self, version: AgentPackageVersion) -> None:
        content = deepcopy(version.content_dict())
        expected_id = package_version_id_for_content(content)
        if version.package_version_id != expected_id:
            raise StorageIntegrityError(
                f"AgentPackageVersion content-address 校验失败: {version.package_version_id}"
            )
        with self._lock:
            existing = self._packages.get(version.package_version_id)
            if existing is not None:
                if existing.content_dict() == content:
                    return
                raise StorageIntegrityError(
                    f"AgentPackageVersion ID 已存在但内容不同: {version.package_version_id}"
                )
            self._packages[version.package_version_id] = decode_agent_package_content(
                package_version_id=version.package_version_id,
                content=deepcopy(content),
                created_at=version.created_at,
            )

    def load_agent_package_version(
        self, package_version_id: str
    ) -> AgentPackageVersion | None:
        with self._lock:
            version = self._packages.get(package_version_id)
            if version is None:
                return None
            return decode_agent_package_content(
                package_version_id=version.package_version_id,
                content=deepcopy(version.content_dict()),
                created_at=version.created_at,
            )

    def insert_artifact(self, artifact: Artifact) -> None:
        with self._lock:
            existing = self._artifacts.get(artifact.artifact_id)
            if existing is not None and existing != artifact:
                raise StorageIntegrityError(
                    f"Artifact ID 已存在但内容不同: {artifact.artifact_id}"
                )
            self._artifacts[artifact.artifact_id] = artifact

    def load_artifact(self, artifact_id: str) -> Artifact | None:
        with self._lock:
            return self._artifacts.get(artifact_id)

    # Operation ---------------------------------------------------------
    def load_operation(self, operation_id: str) -> SessionOperation | None:
        with self._lock:
            return self._operations.get(operation_id)

    def list_operations(self, *, session_id: str) -> tuple[SessionOperation, ...]:
        with self._lock:
            values = [
                item
                for item in self._operations.values()
                if item.session_id == session_id
            ]
            values.sort(
                key=lambda item: (item.accepted_at.timestamp(), item.operation_id)
            )
            return tuple(values)

    def load_run_state(self, operation_id: str) -> AgentRunState | None:
        with self._lock:
            return self._run_states.get(operation_id)

    def commit_run_transition(
        self,
        *,
        state: AgentRunState,
        expected_revision: int,
        node: ConversationNode | None,
        updated_at: datetime,
    ) -> bool:
        """唯一 State CAS、可选 Node 与 Session 指针原子提交入口。"""
        with self._lock:
            current = self._run_states.get(state.operation_id)
            if current is None or current.revision != expected_revision:
                return False
            if state.revision != expected_revision + 1:
                raise StorageIntegrityError("AgentRunState revision 必须恰好递增 1")
            operation = self._operations[state.operation_id]
            session = self._require_session_unlocked(operation.session_id)
            if session.active_operation_id != state.operation_id:
                return False
            if self._transition_blocked_by_pending_step_messages_unlocked(
                session_id=session.session_id,
                current=current,
                next_state=state,
            ):
                return False
            if (
                current.status == "cancelling"
                and state.status == "cancelled"
                and not self._cancellation_ready_unlocked(state.operation_id)
            ):
                return False
            if node is not None:
                if node.node_id in self._nodes:
                    raise StorageIntegrityError(
                        f"ConversationNode 已存在: {node.node_id}"
                    )
                if node.session_id != operation.session_id:
                    raise StorageIntegrityError(
                        "ConversationNode 不属于 Operation Session"
                    )
                if node.parent_node_id != session.active_node_id:
                    return False
                self._validate_content_artifacts_unlocked(node.content)
            self._validate_state_references_unlocked(state, pending_node=node)
            if node is not None and node.node_id not in self._state_node_ids(state):
                raise StorageIntegrityError(
                    "新 ConversationNode 必须被 AgentRunState 引用"
                )

            if node is not None:
                self._nodes[node.node_id] = node
            self._run_states[state.operation_id] = state
            self._sessions[session.session_id] = replace(
                session,
                active_node_id=(
                    node.node_id if node is not None else session.active_node_id
                ),
                active_operation_id=(
                    None
                    if state.status in {"succeeded", "failed", "cancelled"}
                    else session.active_operation_id
                ),
                updated_at=updated_at,
            )
            return True

    def claim_step_messages(
        self,
        *,
        message_ids: tuple[str, ...],
        state: AgentRunState,
        expected_revision: int,
        updated_at: datetime,
    ) -> bool:
        with self._lock:
            if not message_ids or state.revision != expected_revision + 1:
                return False
            current = self._run_states.get(state.operation_id)
            operation = self._operations.get(state.operation_id)
            if current is None or operation is None:
                return False
            session = self._require_session_unlocked(operation.session_id)
            step = state.current_step
            if (
                current.revision != expected_revision
                or current.operation_id != state.operation_id
                or state.status != "running"
                or step is None
                or step.phase != "preparing_request"
                or session.archived_at is not None
                or session.active_operation_id != state.operation_id
            ):
                return False
            if len(set(message_ids)) != len(message_ids):
                return False
            messages: list[InboxMessage] = []
            for message_id in message_ids:
                message = self._inbox.get(message_id)
                if (
                    message is None
                    or message.session_id != operation.session_id
                    or message.status != "pending"
                    or message.delivery not in {"steer", "inject"}
                    or message.message_id in self._nodes
                ):
                    return False
                messages.append(message)
            if [item.sequence for item in messages] != sorted(
                item.sequence for item in messages
            ):
                return False
            parent_node_id = session.active_node_id
            nodes: list[ConversationNode] = []
            try:
                for message in messages:
                    node = ConversationNode(
                        node_id=message.message_id,
                        session_id=operation.session_id,
                        parent_node_id=parent_node_id,
                        content_type="agent_message",
                        content=message.message,
                        created_at=message.created_at,
                    )
                    self._validate_content_artifacts_unlocked(node.content)
                    nodes.append(node)
                    parent_node_id = node.node_id
                self._validate_state_references_unlocked(state, pending_node=None)
            except (StorageIntegrityError, ValueError, TypeError):
                return False

            for node in nodes:
                self._nodes[node.node_id] = node
            for message in messages:
                self._inbox[message.message_id] = replace(
                    message,
                    status="claimed",
                    claimed_operation_id=state.operation_id,
                    claimed_step_id=step.step_id,
                    handled_at=updated_at,
                )
            self._run_states[state.operation_id] = state
            self._sessions[session.session_id] = replace(
                session,
                active_node_id=nodes[-1].node_id,
                updated_at=updated_at,
            )
            return True

    def load_delegation(self, child_session_id: str) -> AgentDelegation | None:
        with self._lock:
            return self._delegations.get(child_session_id)

    def insert_delegation(self, delegation: AgentDelegation) -> None:
        with self._lock:
            self._validate_delegation_unlocked(delegation)
            if delegation.child_session_id in self._delegations:
                if self._delegations[delegation.child_session_id] == delegation:
                    return
                raise StorageIntegrityError("child Session 已有 AgentDelegation")
            if any(
                item.parent_tool_call_id == delegation.parent_tool_call_id
                for item in self._delegations.values()
            ):
                raise StorageIntegrityError("parent_tool_call_id 已经委派")
            if any(
                item.initial_message_id == delegation.initial_message_id
                for item in self._delegations.values()
            ):
                raise StorageIntegrityError("initial_message_id 已经委派")
            self._delegations[delegation.child_session_id] = delegation

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
    ) -> AgentDelegation:
        with self._lock:
            operation = self._operations.get(parent_operation_id)
            state = self._run_states.get(parent_operation_id)
            if operation is None or state is None:
                raise StorageIntegrityError("parent Operation/State 不存在")
            parent = self._sessions.get(operation.session_id)
            workspace = self._workspaces.get(parent.workspace_id) if parent else None
            if parent is None or workspace is None:
                raise StorageIntegrityError("parent Session/Workspace 不存在")
            if parent.active_operation_id != parent_operation_id:
                raise StorageConflictError("parent Session 未指向 parent Operation")
            if state.status != "running":
                raise StorageConflictError("parent Operation 必须处于 running")
            if (
                operation.workspace_binding.workspace_id != parent.workspace_id
                or operation.workspace_binding.working_directory != parent.cwd
            ):
                raise StorageIntegrityError("parent Operation.workspace_binding 漂移")
            step = state.current_step
            call = (
                next(
                    (
                        item
                        for item in step.tool_calls
                        if item.tool_call_id == parent_tool_call_id
                    ),
                    None,
                )
                if step is not None
                else None
            )
            if (
                step is None
                or step.step_id != parent_step_id
                or step.phase != "awaiting_tools"
                or call is None
                or call.status != "intent_recorded"
                or not isinstance(call.execution_intent, DelegateAgentIntent)
            ):
                raise StorageConflictError(
                    "parent ToolCall 不满足 delegation acceptance"
                )
            child_package = self._packages.get(
                call.execution_intent.child_package_version_id
            )
            parent_package = self._packages.get(operation.agent_package_version_id)
            if child_package is None or parent_package is None:
                raise StorageIntegrityError("parent/child AgentPackageVersion 不存在")
            depth = self._delegation_depth_unlocked(operation.session_id)
            if depth >= parent_package.runtime_policy.max_delegation_depth:
                raise StorageConflictError("已达到最大 delegation depth")
            expected_source = AgentMessageSource(
                sender_session_id=operation.session_id,
                sender_operation_id=operation.operation_id,
                form="followup",
            )
            self._validate_delegation_request_unlocked(
                operation=operation,
                parent=parent,
                child_package=child_package,
                child_session=child_session,
                delegation=delegation,
                message_id=message_id,
                parent_step_id=parent_step_id,
                parent_tool_call_id=parent_tool_call_id,
                message=message,
                source=source,
                expected_source=expected_source,
            )
            existing = next(
                (
                    item
                    for item in self._delegations.values()
                    if item.parent_tool_call_id == parent_tool_call_id
                ),
                None,
            )
            if existing is not None:
                self._validate_idempotent_delegation_unlocked(
                    existing=existing,
                    parent_operation_id=parent_operation_id,
                    parent_step_id=parent_step_id,
                    parent_tool_call_id=parent_tool_call_id,
                    child_package=child_package,
                    workspace_id=parent.workspace_id,
                    cwd=parent.cwd,
                    message=message,
                    source=source,
                )
                return existing
            if child_session.session_id in self._sessions:
                raise StorageConflictError("child Session ID 已存在")
            if message_id in self._inbox:
                raise StorageConflictError("initial InboxMessage ID 已存在")
            sequence = (
                max(
                    (
                        item.sequence
                        for item in self._inbox.values()
                        if item.session_id == child_session.session_id
                    ),
                    default=0,
                )
                + 1
            )
            stored = InboxMessage(
                message_id=message_id,
                session_id=child_session.session_id,
                sequence=sequence,
                delivery="followup",
                message=message,
                source=source,
                created_at=created_at,
            )
            self._validate_content_artifacts_unlocked(message)
            self._sessions[child_session.session_id] = child_session
            self._inbox[message_id] = stored
            self._delegations[child_session.session_id] = delegation
            return delegation

    def _delegation_depth_unlocked(self, session_id: str) -> int:
        depth = 0
        seen: set[str] = set()
        while session_id not in seen:
            seen.add(session_id)
            delegation = self._delegations.get(session_id)
            if delegation is None:
                return depth
            depth += 1
            operation = self._operations.get(delegation.parent_operation_id)
            if operation is None:
                raise StorageIntegrityError("delegation parent Operation 不存在")
            session_id = operation.session_id
        raise StorageIntegrityError("delegation parent 链存在环")

    def _validate_delegation_request_unlocked(
        self,
        *,
        operation,
        parent,
        child_package,
        child_session,
        delegation,
        message_id,
        parent_step_id,
        parent_tool_call_id,
        message,
        source,
        expected_source,
    ) -> None:
        if delegation.parent_operation_id != operation.operation_id:
            raise StorageIntegrityError("delegation parent_operation_id 不匹配")
        if delegation.child_session_id != child_session.session_id:
            raise StorageIntegrityError("delegation child_session_id 不匹配")
        if (
            delegation.parent_step_id != parent_step_id
            or delegation.parent_tool_call_id != parent_tool_call_id
            or delegation.initial_message_id != message_id
        ):
            raise StorageIntegrityError("delegation 身份字段不匹配")
        if source != expected_source:
            raise StorageIntegrityError("initial InboxMessage source 不匹配")
        if child_session.agent_id != child_package.agent_id:
            raise StorageIntegrityError("child Session.agent_id 不匹配 child Package")
        if (
            child_session.workspace_id != parent.workspace_id
            or child_session.cwd != parent.cwd
            or any(
                value is not None
                for value in (
                    child_session.active_node_id,
                    child_session.active_operation_id,
                    child_session.title,
                    child_session.title_source,
                    child_session.archived_at,
                )
            )
        ):
            raise StorageIntegrityError("child Session 不是继承 Workspace 的空 Session")

    def _validate_idempotent_delegation_unlocked(
        self,
        *,
        existing,
        parent_operation_id,
        parent_step_id,
        parent_tool_call_id,
        child_package,
        workspace_id,
        cwd,
        message,
        source,
    ) -> None:
        child = self._sessions.get(existing.child_session_id)
        initial = self._inbox.get(existing.initial_message_id)
        if (
            child is None
            or initial is None
            or existing.parent_operation_id != parent_operation_id
            or existing.parent_step_id != parent_step_id
            or existing.parent_tool_call_id != parent_tool_call_id
            or child.agent_id != child_package.agent_id
            or child.workspace_id != workspace_id
            or child.cwd != cwd
            or initial.message != message
            or initial.source != source
        ):
            raise StorageConflictError(
                "同一 parent ToolCall 的 delegation 请求语义冲突"
            )

    def find_delegation_by_parent_tool_call(
        self, parent_tool_call_id: str
    ) -> AgentDelegation | None:
        with self._lock:
            return next(
                (
                    item
                    for item in self._delegations.values()
                    if item.parent_tool_call_id == parent_tool_call_id
                ),
                None,
            )

    def list_delegations(
        self, *, parent_operation_id: str
    ) -> tuple[AgentDelegation, ...]:
        with self._lock:
            values = [
                item
                for item in self._delegations.values()
                if item.parent_operation_id == parent_operation_id
            ]
            values.sort(
                key=lambda item: (item.created_at.timestamp(), item.child_session_id)
            )
            return tuple(values)

    def accept_operation(
        self,
        *,
        operation: SessionOperation,
        state: AgentRunState,
        expected_node_id: str | None,
    ) -> bool:
        """原子 claim Inbox、写入输入 Node/Operation/State 并移动 active node。"""
        with self._lock:
            session_id = operation.session_id
            session = self._require_session_unlocked(session_id)
            if state.operation_id != operation.operation_id:
                raise StorageIntegrityError("Operation 与 State 不匹配")
            if (
                session.archived_at is not None
                or session.active_operation_id is not None
            ):
                return False
            if session.active_node_id != expected_node_id:
                return False
            message = self._inbox.get(operation.input_node_id)
            if (
                message is None
                or message.session_id != session_id
                or message.status != "pending"
            ):
                return False
            self._validate_operation_unlocked(operation, allow_pending_input=True)
            if operation.input_node_id in self._nodes:
                raise StorageIntegrityError(
                    "Operation input_node_id 已存在为 ConversationNode"
                )
            if state.revision != 1 or state.status != "queued":
                raise StorageIntegrityError(
                    "新 Operation 必须从 revision=1、queued 开始"
                )
            # SessionOperation 的 package agent_id 必须与 Session agent_id 相同。
            package = self._packages[operation.agent_package_version_id]
            if package.agent_id != session.agent_id:
                raise StorageIntegrityError(
                    "AgentPackageVersion.agent_id 与 Session 不匹配"
                )
            self._validate_state_nodes_unlocked(state)
            self._validate_content_artifacts_unlocked(message.message)
            node = ConversationNode(
                node_id=message.message_id,
                session_id=session_id,
                parent_node_id=expected_node_id,
                content_type="agent_message",
                content=message.message,
                created_at=message.created_at,
            )
            self._nodes[node.node_id] = node
            self._operations[operation.operation_id] = operation
            self._run_states[state.operation_id] = state
            self._inbox[message.message_id] = replace(
                message,
                status="claimed",
                claimed_operation_id=operation.operation_id,
                handled_at=operation.accepted_at,
            )
            self._sessions[session_id] = replace(
                session,
                active_node_id=node.node_id,
                active_operation_id=operation.operation_id,
                updated_at=operation.accepted_at,
            )
            return True

    # Validation/deletion helpers --------------------------------------
    def _require_session_unlocked(self, session_id: str) -> ConversationSession:
        session = self._sessions.get(session_id)
        if session is None:
            raise LookupError(f"ConversationSession 不存在: {session_id}")
        return session

    def _validate_node_unlocked(self, node: ConversationNode) -> None:
        self._require_session_unlocked(node.session_id)
        if node.parent_node_id is not None:
            parent = self._nodes.get(node.parent_node_id)
            if parent is None or parent.session_id != node.session_id:
                raise StorageIntegrityError("ConversationNode parent 不属于 Session")

    def _validate_content_artifacts_unlocked(
        self, content: AgentMessage | HistoryCompaction
    ) -> None:
        if isinstance(content, HistoryCompaction):
            return
        for block in content.content:
            if (
                isinstance(block, ArtifactBlock)
                and block.artifact.artifact_id not in self._artifacts
            ):
                raise StorageIntegrityError(
                    f"Artifact 不存在: {block.artifact.artifact_id}"
                )

    def _validate_operation_unlocked(
        self, operation: SessionOperation, *, allow_pending_input: bool = False
    ) -> None:
        session = self._require_session_unlocked(operation.session_id)
        if operation.agent_package_version_id not in self._packages:
            raise StorageIntegrityError("AgentPackageVersion 不存在")
        package = self._packages[operation.agent_package_version_id]
        if package.agent_id != session.agent_id:
            raise StorageIntegrityError(
                "AgentPackageVersion.agent_id 与 Session 不匹配"
            )
        if operation.workspace_binding.workspace_id != session.workspace_id:
            raise StorageIntegrityError("WorkspaceBinding 与 Session 不匹配")
        node = self._nodes.get(operation.input_node_id)
        if node is not None:
            if node.session_id != operation.session_id:
                raise StorageIntegrityError("input_node_id 不属于 Operation Session")
            return
        if allow_pending_input:
            message = self._inbox.get(operation.input_node_id)
            if message is None or message.session_id != operation.session_id:
                raise StorageIntegrityError("input_node_id 不属于 Operation Session")
            return
        raise StorageIntegrityError("input_node_id 不属于 Operation Session")

    def _validate_state_nodes_unlocked(self, state: AgentRunState) -> None:
        self._validate_state_references_unlocked(state, pending_node=None)

    def _transition_blocked_by_pending_step_messages_unlocked(
        self,
        *,
        session_id: str,
        current: AgentRunState,
        next_state: AgentRunState,
    ) -> bool:
        has_pending = any(
            item.session_id == session_id
            and item.status == "pending"
            and item.delivery in {"steer", "inject"}
            for item in self._inbox.values()
        )
        current_step = current.current_step
        next_step = next_state.current_step
        intent_or_terminal = (
            current_step is not None
            and next_step is not None
            and current_step.phase == "preparing_request"
            and next_step.phase == "request_ready"
        ) or next_state.status == "succeeded"
        if intent_or_terminal:
            return has_pending
        if (
            current_step is not None
            and current_step.phase == "awaiting_tools"
            and not current_step.tool_calls
            and next_state.status == "running"
            and next_step is None
        ):
            return not has_pending
        return False

    def _cancellation_graph_unlocked(self, root_operation_id: str) -> dict[str, object]:
        """沿不可变 Delegation 关系返回取消所需的窄图投影。"""
        operation_ids: set[str] = {root_operation_id}
        descendants_by_operation: dict[str, set[str]] = {}
        visited: set[str] = set()

        def visit(operation_id: str) -> None:
            if operation_id in visited:
                return
            visited.add(operation_id)
            for delegation in self._delegations.values():
                if delegation.parent_operation_id != operation_id:
                    continue
                child_session_id = delegation.child_session_id
                descendants = descendants_by_operation.setdefault(operation_id, set())
                descendants.add(child_session_id)
                child_operations = [
                    item
                    for item in self._operations.values()
                    if item.session_id == child_session_id
                ]
                for child_operation in child_operations:
                    operation_ids.add(child_operation.operation_id)
                    visit(child_operation.operation_id)
                    descendants.update(
                        descendants_by_operation.get(child_operation.operation_id, ())
                    )

        visit(root_operation_id)
        return {
            "operation_ids": operation_ids,
            "descendants_by_operation": descendants_by_operation,
        }

    def _is_cancellation_message_unlocked(
        self, message: InboxMessage, graph: dict[str, object]
    ) -> bool:
        source = message.source
        if not isinstance(source, AgentMessageSource):
            return False
        descendants_by_operation = graph["descendants_by_operation"]
        if not isinstance(descendants_by_operation, dict):
            return False
        target_sessions = descendants_by_operation.get(source.sender_operation_id)
        sender_operation = self._operations.get(source.sender_operation_id)
        return (
            sender_operation is not None
            and sender_operation.session_id == source.sender_session_id
            and source.form == message.delivery
            and isinstance(target_sessions, set)
            and message.session_id in target_sessions
        )

    def _cancellation_ready_unlocked(self, root_operation_id: str) -> bool:
        graph = self._cancellation_graph_unlocked(root_operation_id)
        for operation_id in graph["operation_ids"] - {root_operation_id}:
            state = self._run_states.get(operation_id)
            if state is None or state.status not in {
                "succeeded",
                "failed",
                "cancelled",
            }:
                return False
        return not any(
            message.status == "pending"
            and self._is_cancellation_message_unlocked(message, graph)
            for message in self._inbox.values()
        )

    @staticmethod
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

    def _validate_state_references_unlocked(
        self,
        state: AgentRunState,
        *,
        pending_node: ConversationNode | None,
    ) -> None:
        operation = self._operations.get(state.operation_id)
        if operation is None:
            return
        for node_id in self._state_node_ids(state):
            node = (
                pending_node
                if pending_node is not None and pending_node.node_id == node_id
                else self._nodes.get(node_id)
            )
            if node is None or node.session_id != operation.session_id:
                raise StorageIntegrityError(
                    f"AgentRunState 引用的 Node 不属于 Operation Session: {node_id}"
                )

    def _validate_delegation_unlocked(self, delegation: AgentDelegation) -> None:
        if delegation.child_session_id not in self._sessions:
            raise StorageIntegrityError("child Session 不存在")
        if delegation.parent_operation_id not in self._operations:
            raise StorageIntegrityError("parent Operation 不存在")
        message = self._inbox.get(delegation.initial_message_id)
        if message is None or message.session_id != delegation.child_session_id:
            raise StorageIntegrityError("initial_message_id 不属于 child Session")

    def _assert_idle_unlocked(self, session_id: str) -> None:
        session = self._require_session_unlocked(session_id)
        if session.active_operation_id is not None:
            raise StorageIntegrityError("Session 仍有 active Operation")

    def _assert_deletable_unlocked(self, session_id: str) -> None:
        session = self._require_session_unlocked(session_id)
        if session.archived_at is None:
            raise StorageIntegrityError("删除 Session 前必须先归档")
        self._assert_idle_unlocked(session_id)
        if any(
            item.session_id == session_id and item.status == "pending"
            for item in self._inbox.values()
        ):
            raise StorageIntegrityError("存在 pending InboxMessage，不能删除 Session")

    def _descendant_sessions_unlocked(self, root: str) -> set[str]:
        result = {root}
        changed = True
        while changed:
            changed = False
            for delegation in self._delegations.values():
                parent = self._operations.get(delegation.parent_operation_id)
                if (
                    parent is not None
                    and parent.session_id in result
                    and delegation.child_session_id not in result
                ):
                    result.add(delegation.child_session_id)
                    changed = True
        return result

    def _operation_has_session_unlocked(
        self, delegation: AgentDelegation, session_id: str
    ) -> bool:
        operation = self._operations.get(delegation.parent_operation_id)
        return operation is not None and operation.session_id == session_id

    def _delete_sessions_unlocked(self, session_ids: set[str]) -> None:
        for child_session_id, delegation in list(self._delegations.items()):
            parent = self._operations.get(delegation.parent_operation_id)
            if delegation.child_session_id in session_ids or (
                parent is not None and parent.session_id in session_ids
            ):
                del self._delegations[child_session_id]
        for operation_id, operation in list(self._operations.items()):
            if operation.session_id in session_ids:
                self._operations.pop(operation_id)
                self._run_states.pop(operation_id, None)
        for node_id, node in list(self._nodes.items()):
            if node.session_id in session_ids:
                del self._nodes[node_id]
        for message_id, message in list(self._inbox.items()):
            if message.session_id in session_ids:
                del self._inbox[message_id]
        for target in session_ids:
            self._sessions.pop(target, None)


__all__ = ["InMemoryRuntimeStore"]
