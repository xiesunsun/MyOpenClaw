"""从 RuntimeStore 读取可靠业务事实。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from pickel.agents.agent_package import AgentPackageVersion
from pickel.conversations.agent_message import (
    AssistantMessage,
    ToolResultMessage,
    agent_message_to_dict,
)
from pickel.conversations.content_blocks import ToolCallBlock
from pickel.conversations.conversation_node import ConversationNode
from pickel.conversations.conversation_session import ConversationSession
from pickel.inbox.message import InboxMessage
from pickel.model_calls.model_call import ModelCall
from pickel.operations.agent_delegation import AgentDelegation
from pickel.operations.agent_run_state import AgentRunState
from pickel.operations.session_operation import SessionOperation


class FactStore(Protocol):
    def load_session(self, session_id: str) -> ConversationSession | None: ...

    def list_sessions(
        self,
        *,
        cwd: Any = None,
        agent_id: str | None = None,
        include_archived: bool = False,
        limit: int | None = None,
    ) -> tuple[ConversationSession, ...]: ...

    def load_operation(self, operation_id: str) -> SessionOperation | None: ...

    def list_operations(self, *, session_id: str) -> tuple[SessionOperation, ...]: ...

    def load_run_state(self, operation_id: str) -> AgentRunState | None: ...

    def load_model_call(self, model_call_id: str) -> ModelCall | None: ...

    def list_model_calls(
        self,
        *,
        session_id: str,
        operation_id: str | None = None,
        step_id: str | None = None,
    ) -> tuple[ModelCall, ...]: ...

    def load_node(self, node_id: str) -> ConversationNode | None: ...

    def list_branch_nodes(
        self,
        session_id: str,
        leaf_node_id: str | None = None,
    ) -> tuple[ConversationNode, ...]: ...

    def load_delegation(self, child_session_id: str) -> AgentDelegation | None: ...

    def list_delegations(
        self,
        *,
        parent_operation_id: str | None = None,
        parent_step_id: str | None = None,
    ) -> tuple[AgentDelegation, ...]: ...

    def load_message(self, message_id: str) -> InboxMessage | None: ...

    def load_agent_package_version(
        self, package_version_id: str
    ) -> AgentPackageVersion | None: ...


@dataclass(frozen=True)
class OperationFacts:
    """单个 Operation 及其相关的可靠上下文事实。"""

    operation: SessionOperation
    run_state: AgentRunState | None
    package_version: AgentPackageVersion | None
    model_calls: tuple[ModelCall, ...]
    delegations: tuple[AgentDelegation, ...]
    input_node: ConversationNode | None
    final_node: ConversationNode | None
    branch_nodes: tuple[ConversationNode, ...] = ()
    tool_calls: tuple["OperationToolFact", ...] = ()


@dataclass(frozen=True)
class OperationToolFact:
    """Conversation Tree 中一组可靠的 ToolCall/ToolResult 事实。"""

    tool_call_id: str
    name: str
    arguments: dict[str, Any]
    result: dict[str, Any] | None
    is_error: bool | None
    step_id: str | None
    step_sequence: int | None
    call_node_id: str
    result_node_id: str | None
    source: str = "conversation_node"
    reliability: str = "fact"


class OperationFactReader:
    """只读取可靠业务事实的窄领域 Reader。"""

    def __init__(self, store: FactStore) -> None:
        self._store = store

    def read_session(self, session_id: str) -> ConversationSession | None:
        return self._store.load_session(session_id)

    def read_session_operations(self, session_id: str) -> tuple[SessionOperation, ...]:
        return self._store.list_operations(session_id=session_id)

    def read_operation(self, operation_id: str) -> SessionOperation | None:
        return self._store.load_operation(operation_id)

    def read_run_state(self, operation_id: str) -> AgentRunState | None:
        return self._store.load_run_state(operation_id)

    def read_model_calls(
        self,
        session_id: str,
        *,
        operation_id: str | None = None,
        step_id: str | None = None,
    ) -> tuple[ModelCall, ...]:
        return self._store.list_model_calls(
            session_id=session_id,
            operation_id=operation_id,
            step_id=step_id,
        )

    def read_model_call(self, model_call_id: str) -> ModelCall | None:
        """按稳定 ModelCall 身份读取一条可靠事实。"""
        return self._store.load_model_call(model_call_id)

    def read_delegations(
        self,
        *,
        parent_operation_id: str | None = None,
        parent_step_id: str | None = None,
    ) -> tuple[AgentDelegation, ...]:
        """读取 Child Agent 委派事实。"""
        if parent_operation_id is None:
            return ()
        delegations = self._store.list_delegations(
            parent_operation_id=parent_operation_id,
        )
        if parent_step_id is not None:
            delegations = tuple(
                d for d in delegations if d.parent_step_id == parent_step_id
            )
        return delegations

    def read_active_branch_nodes(
        self, session_id: str, *, active_node_id: str | None = None
    ) -> tuple[ConversationNode, ...]:
        return self._store.list_branch_nodes(
            session_id,
            active_node_id,
        )

    def read_operation_branch_nodes(
        self,
        operation: SessionOperation,
        run_state: AgentRunState | None,
        session: ConversationSession | None = None,
    ) -> tuple[ConversationNode, ...]:
        """读取并截取当前 Operation 的 input→leaf 分支。"""
        if session is None:
            session = self.read_session(operation.session_id)
        leaf_node_id = (
            run_state.final_assistant_node_id
            if run_state is not None and run_state.final_assistant_node_id is not None
            else (
                session.active_node_id
                if session is not None
                else operation.input_node_id
            )
        )
        if leaf_node_id is not None and self.read_node(leaf_node_id) is None:
            # 损坏/旧数据可能只保留了 State 的 final ID；不要让观测读取
            # 整个 Operation 失败，退回 Session 当前可靠 leaf。
            leaf_node_id = session.active_node_id if session is not None else None
        if leaf_node_id is None:
            return ()
        branch = self._store.list_branch_nodes(
            operation.session_id,
            leaf_node_id,
        )
        for index, node in enumerate(branch):
            if node.node_id == operation.input_node_id:
                return branch[index:]
        return ()

    @staticmethod
    def _tool_step(
        tool_call_id: str,
        run_state: AgentRunState | None,
        model_calls: tuple[ModelCall, ...],
        call_node: ConversationNode,
    ) -> tuple[str | None, int | None]:
        if run_state is not None and run_state.current_step is not None:
            for item in run_state.current_step.tool_calls:
                if item.tool_call_id == tool_call_id:
                    return (
                        run_state.current_step.step_id,
                        run_state.current_step.step_sequence,
                    )
        # 终态后 current_step 已清空，不能用时间近似推断 Step 身份；
        # Projector 会从已保存的 ResponseContent ToolCallBlock 做精确匹配。
        return None, None

    def read_operation_tool_calls(
        self,
        *,
        branch_nodes: tuple[ConversationNode, ...],
        run_state: AgentRunState | None,
        model_calls: tuple[ModelCall, ...],
    ) -> tuple[OperationToolFact, ...]:
        """从可靠 ConversationNode 读取 ToolCall 与其 Result 配对。"""
        results: dict[str, tuple[ConversationNode, ToolResultMessage]] = {}
        for node in branch_nodes:
            if node.content_type == "agent_message" and isinstance(
                node.content, ToolResultMessage
            ):
                results[node.content.tool_call_id] = (node, node.content)

        tool_facts: list[OperationToolFact] = []
        for node in branch_nodes:
            if node.content_type != "agent_message" or not isinstance(
                node.content, AssistantMessage
            ):
                continue
            for block in node.content.content:
                if not isinstance(block, ToolCallBlock):
                    continue
                result_entry = results.get(block.id)
                result_node = result_entry[0] if result_entry else None
                result_message = result_entry[1] if result_entry else None
                step_id, step_sequence = self._tool_step(
                    block.id, run_state, model_calls, node
                )
                tool_facts.append(
                    OperationToolFact(
                        tool_call_id=block.id,
                        name=block.name,
                        arguments=dict(block.arguments),
                        result=(
                            agent_message_to_dict(result_message)
                            if result_message is not None
                            else None
                        ),
                        is_error=(
                            result_message.is_error
                            if result_message is not None
                            else None
                        ),
                        step_id=step_id,
                        step_sequence=step_sequence,
                        call_node_id=node.node_id,
                        result_node_id=(result_node.node_id if result_node else None),
                    )
                )
        return tuple(tool_facts)

    def read_node(self, node_id: str) -> ConversationNode | None:
        return self._store.load_node(node_id)

    def read_agent_package_version(
        self, package_version_id: str
    ) -> AgentPackageVersion | None:
        return self._store.load_agent_package_version(package_version_id)

    def read_operation_facts(self, operation_id: str) -> OperationFacts | None:
        operation = self.read_operation(operation_id)
        if operation is None:
            return None
        run_state = self.read_run_state(operation_id)
        package = self.read_agent_package_version(operation.agent_package_version_id)
        model_calls = self.read_model_calls(
            session_id=operation.session_id,
            operation_id=operation_id,
        )
        delegations = self.read_delegations(parent_operation_id=operation_id)
        input_node = self.read_node(operation.input_node_id)
        final_node = (
            self.read_node(run_state.final_assistant_node_id)
            if run_state is not None and run_state.final_assistant_node_id is not None
            else None
        )
        session = self.read_session(operation.session_id)
        branch_nodes = self.read_operation_branch_nodes(operation, run_state, session)
        tool_calls = self.read_operation_tool_calls(
            branch_nodes=branch_nodes,
            run_state=run_state,
            model_calls=model_calls,
        )
        return OperationFacts(
            operation=operation,
            run_state=run_state,
            package_version=package,
            model_calls=model_calls,
            delegations=delegations,
            input_node=input_node,
            final_node=final_node,
            branch_nodes=branch_nodes,
            tool_calls=tool_calls,
        )
