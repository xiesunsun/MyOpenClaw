"""OperationDriver v10 的最小恢复与副作用合同。"""

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import asyncio
from functools import wraps
from unittest.mock import patch

import pytest

from pickel.context.model_context import ModelContext, SystemContent, SystemSection
from pickel.context.model_context_builder import (
    ContextContributions,
    ModelContextBuilder,
)
from pickel.conversations.agent_message import (
    AssistantMessage,
    ModelResponseMetadata,
    ModelUsage,
    ToolResultMessage,
    UserMessage,
)
from pickel.conversations.content_blocks import TextBlock, ToolCallBlock
from pickel.conversations.conversation_node import ConversationNode, HistoryCompaction
from pickel.operations.agent_run_state import (
    AgentRunError,
    AgentRunState,
    DelegateAgentIntent,
    ModelStepState,
    ToolApproval,
    ToolApprovalDecision,
    ToolCallState,
)
from pickel.operations.session_operation import SessionOperation
from pickel.hooks.decisions import PreToolUseDecision
from pickel.providers.stream import StreamCompleted, TextDelta
from pickel.model_calls.model_call import ModelCall, ModelCallError
from pickel.providers.prepared import PreparedModelCall
from pickel.model_calls.service import AgentPreparedModelCall
from pickel.shared.execution_identity import ExecutionIdentity
from pickel.runtime.operation_driver import OperationDriver
from pickel.runtime.runtime_effects import RuntimeEffects
from pickel.runtime.runtime_events import ToolCallCompleted, ToolCallStarted
from pickel.agents.agent_package_loader import PackageLoadError
from pickel.workspaces.workspace_binding import WorkspaceBinding
from pickel.agents.agent_package import AgentDelegationPolicy, AgentRuntimePolicy


def _run_async(function):
    @wraps(function)
    def wrapper(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))

    return wrapper


class _Conversation:
    def list_active_branch_nodes(self, *, session_id):
        return ()

    def load_conversation_session(self, session_id):
        return SimpleNamespace(active_node_id=None)

    def list_branch_nodes(self, *, session_id, leaf_node_id):
        if leaf_node_id is None:
            return ()
        return [
            ConversationNode(
                node_id="node-1",
                session_id=session_id,
                parent_node_id=None,
                content_type="agent_message",
                content=UserMessage(),
                created_at=datetime.now(timezone.utc),
            ),
            ConversationNode(
                node_id=leaf_node_id,
                session_id=session_id,
                parent_node_id="node-1",
                content_type="agent_message",
                content=AssistantMessage(content=(TextBlock(text="done"),)),
                created_at=datetime.now(timezone.utc),
            ),
        ]


class _RecoveredCompactionConversation(_Conversation):
    """模拟提交压缩事实后、二次 preflight 前进程恢复。"""

    def list_active_branch_nodes(self, *, session_id):
        return [
            ConversationNode(
                node_id="node-1",
                session_id=session_id,
                parent_node_id=None,
                content_type="agent_message",
                content=UserMessage(),
                created_at=datetime.now(timezone.utc),
            ),
            ConversationNode(
                node_id="compaction-1",
                session_id=session_id,
                parent_node_id="node-1",
                content_type="history_compaction",
                content=HistoryCompaction(
                    summary="previous summary", first_kept_node_id="node-1"
                ),
                created_at=datetime.now(timezone.utc),
            ),
        ]


class _CompactionConversation(_Conversation):
    def __init__(self):
        self.nodes = [
            ConversationNode(
                node_id="node-1",
                session_id="session-1",
                parent_node_id=None,
                content_type="agent_message",
                content=UserMessage(),
                created_at=datetime.now(timezone.utc),
            )
        ]

    def list_active_branch_nodes(self, *, session_id):
        return tuple(self.nodes)

    def append_history_compaction(self, *, session_id, content):
        node = ConversationNode(
            node_id="compaction-1",
            session_id=session_id,
            parent_node_id=self.nodes[-1].node_id,
            content_type="history_compaction",
            content=content,
            created_at=datetime.now(timezone.utc),
        )
        self.nodes.append(node)
        return node


class _UsageConversation:
    def __init__(self, nodes, *, active_leaf):
        self.nodes = {node.node_id: node for node in nodes}
        self.active_leaf = active_leaf
        self.branch_calls = []

    def load_conversation_session(self, session_id):
        return SimpleNamespace(active_node_id=self.active_leaf)

    def list_active_branch_nodes(self, *, session_id):
        return self.list_branch_nodes(
            session_id=session_id, leaf_node_id=self.active_leaf
        )

    def list_branch_nodes(self, *, session_id, leaf_node_id):
        self.branch_calls.append(leaf_node_id)
        if leaf_node_id is None:
            return []
        branch = []
        current = leaf_node_id
        while current is not None:
            node = self.nodes[current]
            branch.append(node)
            current = node.parent_node_id
        branch.reverse()
        return branch


def _usage_nodes():
    metadata = ModelResponseMetadata(
        provider="test",
        model="model",
        usage=ModelUsage(input_tokens=10, output_tokens=4),
        elapsed_ms=12,
    )
    return (
        ConversationNode(
            node_id="node-1",
            session_id="session-1",
            parent_node_id=None,
            content_type="agent_message",
            content=UserMessage(),
            created_at=datetime.now(timezone.utc),
        ),
        ConversationNode(
            node_id="assistant-1",
            session_id="session-1",
            parent_node_id="node-1",
            content_type="agent_message",
            content=AssistantMessage(metadata=metadata),
            created_at=datetime.now(timezone.utc),
        ),
        ConversationNode(
            node_id="assistant-next-operation",
            session_id="session-1",
            parent_node_id="assistant-1",
            content_type="agent_message",
            content=AssistantMessage(metadata=metadata),
            created_at=datetime.now(timezone.utc),
        ),
    )


class _ContextBuilder:
    def build_model_context(self, **kwargs):
        return ModelContext(
            system=SystemContent(), messages=tuple(kwargs["visible_messages"])
        )


class _Provider:
    def __init__(self, messages):
        self.messages = list(messages)
        self.calls = 0
        self.contexts = []

    def prepare(self, context):
        self.contexts.append(context)
        return PreparedModelCall(
            provider="test",
            api_kind="test-wire",
            endpoint="responses",
            requested_model="model",
            body={"model": "model", "stream": True},
        )

    async def count_context_tokens(self, context):
        del context
        return 0

    async def stream_prepared(self, prepared):
        del prepared
        self.calls += 1
        item = self.messages.pop(0)
        if isinstance(item, Exception):
            raise item
        yield StreamCompleted(item, provider_response={"attempt": self.calls})


class _PartialOutputFailureProvider(_Provider):
    async def stream_prepared(self, prepared):
        del prepared
        self.calls += 1
        yield TextDelta("partial")
        raise TimeoutError("stream idle after first output")


class _Operations:
    def __init__(self, state, *, commit_transition=True, cas=True, pending=()):
        self.state = state
        self.transition_calls = []
        self.cas = cas
        self.commit_transition_enabled = commit_transition
        self.pending = tuple(pending)
        self.claim_calls = []

    def load_operation(self, operation_id):
        return _operation()

    def load_agent_run_state(self, operation_id):
        return self.state

    def commit_transition(self, *, state, expected_revision, node):
        if not self.commit_transition_enabled:
            return False
        self.transition_calls.append((state, node))
        self.state = state
        return state

    def commit_state(self, *, state, expected_revision):
        if not self.cas:
            return False
        self.state = state
        return True

    def list_pending_step_messages(self, *, session_id):
        assert session_id == "session-1"
        return self.pending

    def claim_step_messages(self, *, message_ids, state, expected_revision, updated_at):
        del updated_at
        self.claim_calls.append((message_ids, state, expected_revision))
        if expected_revision != self.state.revision:
            return False
        self.state = state
        self.pending = ()
        return True


class _FakeModelCallService:
    def __init__(self, operations):
        self.operations = operations
        self.store = self
        self.calls = {}
        self.sequence = 0

    def prepare_or_recover_agent_call(self, *, operation, state, mapper, max_attempts):
        del max_attempts
        step = state.current_step
        assert step is not None and step.request_intent is not None
        self.sequence += 1
        next_state = replace(
            state,
            revision=state.revision + 1,
            current_step=replace(step, request_attempt=step.request_attempt + 1),
        )
        prepared = mapper.prepare(step.request_intent.model_context)
        call = ModelCall(
            model_call_id=f"model-call-{self.sequence}",
            identity=ExecutionIdentity(
                session_id=operation.session_id,
                operation_id=operation.operation_id,
                step_id=step.step_id,
                step_sequence=step.step_sequence,
            ),
            request_attempt=next_state.current_step.request_attempt,
            model_role="primary",
            purpose="agent_step",
            provider=prepared.provider,
            api_kind=prepared.api_kind,
            endpoint=prepared.endpoint,
            requested_model=prepared.requested_model,
            returned_model=None,
            status="prepared",
            request_content_ref="test-content",
            response_content_ref=None,
            context_fingerprint=step.request_intent.context_fingerprint,
            provider_request_id=None,
            http_status=None,
            error=None,
            created_at=datetime.now(timezone.utc),
            started_at=None,
            first_chunk_at=None,
            finished_at=None,
        )
        self.calls[call.model_call_id] = call
        self.operations.transition_calls.append((next_state, None))
        self.operations.state = next_state
        retry_after = (
            next_state.current_step.request_attempt - 1
            if next_state.current_step.request_attempt > 1
            else None
        )
        return AgentPreparedModelCall(
            state=next_state,
            model_call=call,
            prepared=prepared,
            reused=False,
            retry_after_attempt=retry_after,
        )

    def transition_model_call(self, *, model_call, expected_status):
        current = self.calls.get(model_call.model_call_id)
        if current is None or current.status != expected_status:
            return False
        self.calls[model_call.model_call_id] = model_call
        return True

    def mark_failed(self, call, error, *, first_chunk_at=None):
        failed = replace(
            call,
            status="failed",
            first_chunk_at=first_chunk_at,
            finished_at=datetime.now(timezone.utc),
            error=ModelCallError(error.code, str(error), retryable=error.retryable),
            http_status=error.status_code,
        )
        self.calls[call.model_call_id] = failed
        return failed

    def commit_agent_response(self, *, call, response, state, expected_revision, node):
        del call, response, expected_revision
        self.operations.transition_calls.append((state, node))
        self.operations.state = state
        return None

    def commit_agent_processing_failure(
        self,
        *,
        call,
        response,
        state,
        expected_revision,
        node,
        error,
        response_content_ref=None,
    ):
        del expected_revision, node, error, response_content_ref
        self.calls[call.model_call_id] = replace(
            call,
            status="completed",
            response_content_ref="test-response",
            started_at=response.started_at,
            first_chunk_at=response.first_chunk_at,
            finished_at=response.finished_at,
        )
        self.operations.transition_calls.append((state, None))
        self.operations.state = state
        return self.calls[call.model_call_id]


def _operation() -> SessionOperation:
    return SessionOperation(
        operation_id="operation-1",
        session_id="session-1",
        agent_package_version_id="agentpkg_" + "a" * 64,
        workspace_binding=WorkspaceBinding(
            workspace_id="workspace-1",
            working_directory=Path.cwd(),
            allowed_root=Path.cwd(),
        ),
        input_node_id="node-1",
        accepted_at=datetime.now(timezone.utc),
    )


def _queued_state() -> AgentRunState:
    return AgentRunState(
        operation_id="operation-1",
        revision=1,
        status="queued",
        waiting_reason=None,
        completed_step_count=0,
        current_step=None,
        final_assistant_node_id=None,
        error=None,
        cancellation=None,
    )


@_run_async
async def test_recovered_cancelled_child_wakes_direct_parent():
    state = replace(
        _queued_state(),
        status="cancelled",
        cancellation=SimpleNamespace(cause="parent cancelled"),
    )
    operations = _Operations(state)
    operations.parent_session_id = lambda operation_id: "parent-session"
    woken: list[str] = []
    driver = OperationDriver(
        operation_service=operations,
        conversation_service=_Conversation(),
        package_loader=lambda _: pytest.fail("终态恢复不应加载 Package"),
        effects_resolver=lambda _: pytest.fail("终态恢复不应解析 Effects"),
        wake_callback=woken.append,
    )

    result = await driver.drive_operation("operation-1")

    assert result.status == "cancelled"
    assert woken == ["parent-session"]


@pytest.mark.parametrize("status", ["succeeded", "failed", "cancelled"])
@_run_async
async def test_terminal_parent_callback_runs_once_with_wake_fallback(status: str):
    state = replace(
        _queued_state(),
        status=status,
        error=(
            AgentRunError("failed", "failed", False) if status == "failed" else None
        ),
        cancellation=(
            SimpleNamespace(cause="cancelled") if status == "cancelled" else None
        ),
        final_assistant_node_id=("final-node" if status == "succeeded" else None),
    )
    operations = _Operations(state)
    operations.parent_session_id = lambda operation_id: "parent-session"
    woken: list[str] = []
    terminal: list[str] = []
    driver = OperationDriver(
        operation_service=operations,
        conversation_service=_Conversation(),
        package_loader=lambda _: pytest.fail("终态恢复不应加载 Package"),
        effects_resolver=lambda _: pytest.fail("终态恢复不应解析 Effects"),
        wake_callback=woken.append,
        terminal_callback=terminal.append,
    )

    result = await driver.drive_operation("operation-1")

    assert result.status == status
    assert terminal == ["parent-session"]
    assert woken == []


@_run_async
async def test_terminal_wake_callback_failure_does_not_change_result():
    state = replace(
        _queued_state(),
        status="failed",
        error=AgentRunError("failed", "failed", False),
    )
    operations = _Operations(state)
    operations.parent_session_id = lambda operation_id: "parent-session"

    def fail_wake(session_id: str) -> None:
        raise RuntimeError(session_id)

    driver = OperationDriver(
        operation_service=operations,
        conversation_service=_Conversation(),
        package_loader=lambda _: pytest.fail("终态恢复不应加载 Package"),
        effects_resolver=lambda _: pytest.fail("终态恢复不应解析 Effects"),
        wake_callback=fail_wake,
    )

    result = await driver.drive_operation("operation-1")

    assert result.status == "failed"


@_run_async
async def test_context_hides_report_for_root_and_keeps_it_for_delegated_session():
    package = _loaded_package(tool_name="report").version
    package.tools = (
        SimpleNamespace(
            name="report",
            description="report",
            replay_policy="safe",
            source=SimpleNamespace(value="builtin"),
            implementation_ref=SimpleNamespace(name="report"),
            input_schema={"type": "object"},
            output_schema={"type": "object"},
        ),
    )
    operations = _Operations(
        replace(
            _queued_state(),
            status="running",
            current_step=ModelStepState(
                "step-1", 1, "preparing_request", 0, None, None, ()
            ),
        )
    )
    operations.load_delegation = lambda session_id: None
    driver = OperationDriver(
        operation_service=operations,
        conversation_service=_Conversation(),
        package_loader=lambda _: package,
        effects_resolver=lambda _: RuntimeEffects(provider=object()),
        model_context_builder=ModelContextBuilder(),
    )
    root_context = await driver._build_context(
        operation=_operation(),
        state=operations.state,
        package=package,
        effects=RuntimeEffects(provider=object()),
    )
    assert [tool.name for tool in root_context.tools] == []

    operations.load_delegation = lambda session_id: SimpleNamespace(
        parent_session_id="parent-session"
    )
    delegated_context = await driver._build_context(
        operation=_operation(),
        state=operations.state,
        package=package,
        effects=RuntimeEffects(provider=object()),
    )
    assert [tool.name for tool in delegated_context.tools] == ["report"]


@_run_async
async def test_succeeded_recovery_projects_final_node_only():
    conversation = _UsageConversation(
        _usage_nodes(), active_leaf="assistant-next-operation"
    )
    state = replace(
        _queued_state(), status="succeeded", final_assistant_node_id="assistant-1"
    )
    operations = _Operations(state)
    driver = OperationDriver(
        operation_service=operations,
        conversation_service=conversation,
        package_loader=lambda _: pytest.fail("终态恢复不应加载 Package"),
        effects_resolver=lambda _: pytest.fail("终态恢复不应解析 Effects"),
    )

    result = await driver.drive_operation("operation-1")

    assert result.usage is not None
    assert result.usage.steps == 1
    assert result.usage.input_tokens == 10
    assert conversation.branch_calls == ["assistant-1"]


@_run_async
async def test_waiting_projects_current_active_leaf_as_partial_usage():
    call = ToolCallState(
        tool_call_id="tool-1",
        tool_name="run",
        arguments={},
        status="waiting_approval",
        approval=ToolApproval(
            requested_at=datetime.now(timezone.utc),
            requested_by="hook",
            reason=None,
            decision=None,
        ),
        replay_policy="safe",
        execution_intent=None,
        decision_reason=None,
        result_node_id=None,
        is_error=None,
    )
    state = replace(
        _queued_state(),
        status="waiting",
        waiting_reason="tool_approval",
        current_step=ModelStepState(
            "step-1", 1, "awaiting_tools", 1, None, "assistant-1", (call,)
        ),
    )
    conversation = _UsageConversation(_usage_nodes(), active_leaf="assistant-1")
    operations = _Operations(state)
    driver = OperationDriver(
        operation_service=operations,
        conversation_service=conversation,
        package_loader=lambda _: _loaded_package().version,
        effects_resolver=lambda _: RuntimeEffects(
            provider=_Provider([AssistantMessage(content=(TextBlock("unused"),))])
        ),
        model_context_builder=_ContextBuilder(),
    )
    result = await driver.drive_operation("operation-1")

    assert result.status == "waiting"
    assert result.usage is not None
    assert result.usage.steps == 1
    assert conversation.branch_calls == ["assistant-1"]


@_run_async
async def test_waiting_keeps_operation_package_and_terminal_requests_idempotent_release():
    operation = _operation()
    waiting = replace(
        _queued_state(),
        status="waiting",
        waiting_reason="tool_approval",
        current_step=ModelStepState(
            "step-1",
            1,
            "awaiting_tools",
            1,
            None,
            "assistant-1",
            (
                ToolCallState(
                    tool_call_id="tool-1",
                    tool_name="run",
                    arguments={},
                    status="waiting_approval",
                    approval=ToolApproval(
                        requested_at=datetime.now(timezone.utc),
                        requested_by="hook",
                        reason=None,
                        decision=None,
                    ),
                    replay_policy="safe",
                    execution_intent=None,
                    decision_reason=None,
                    result_node_id=None,
                    is_error=None,
                ),
            ),
        ),
    )
    operations = _Operations(waiting)
    loaded_for = []
    effects_for = []
    released = []

    async def release(candidate):
        released.append(candidate)

    driver = OperationDriver(
        operation_service=operations,
        conversation_service=_Conversation(),
        package_loader=lambda candidate: (
            loaded_for.append(candidate) or _loaded_package().version
        ),
        effects_resolver=lambda candidate: (
            effects_for.append(candidate) or RuntimeEffects(provider=_Provider([]))
        ),
        release_operation_package=release,
    )

    result = await driver.drive_operation(operation.operation_id)

    assert result.status == "waiting"
    assert [item.operation_id for item in loaded_for] == [operation.operation_id]
    assert effects_for == loaded_for
    assert released == []

    operations.state = replace(
        waiting,
        revision=waiting.revision + 1,
        status="failed",
        waiting_reason=None,
        current_step=None,
        error=AgentRunError("test_failure", "failed", False),
    )
    result = await driver.drive_operation(operation.operation_id)
    result = await driver.drive_operation(operation.operation_id)

    assert result.status == "failed"
    assert [item.operation_id for item in released] == [
        operation.operation_id,
        operation.operation_id,
    ]


@_run_async
async def test_current_package_failure_captures_leaf_before_terminal_commit():
    conversation = _UsageConversation(_usage_nodes(), active_leaf="assistant-1")
    operations = _Operations(_queued_state())
    driver = OperationDriver(
        operation_service=operations,
        conversation_service=conversation,
        package_loader=lambda _: (_ for _ in ()).throw(
            PackageLoadError(
                "tool_unavailable", _operation().agent_package_version_id, "missing"
            )
        ),
        effects_resolver=lambda _: pytest.fail("Package 失败后不能解析 Effects"),
        model_context_builder=_ContextBuilder(),
    )

    result = await driver.drive_operation("operation-1")

    assert result.status == "failed"
    assert result.usage is not None
    assert result.usage.steps == 1
    assert conversation.branch_calls == ["assistant-1"]


@_run_async
async def test_current_max_steps_failure_captures_leaf_before_terminal_commit():
    package = _loaded_package().version
    package.runtime_policy = AgentRuntimePolicy(max_model_steps=1, context_turn_window=8)
    conversation = _UsageConversation(_usage_nodes(), active_leaf="assistant-1")
    operations = _Operations(
        replace(_queued_state(), status="running", completed_step_count=1)
    )
    driver = OperationDriver(
        operation_service=operations,
        conversation_service=conversation,
        package_loader=lambda _: package,
        effects_resolver=lambda _: RuntimeEffects(
            provider=_Provider([AssistantMessage(content=(TextBlock("unused"),))])
        ),
        model_context_builder=_ContextBuilder(),
    )

    result = await driver.drive_operation("operation-1")

    assert result.status == "failed"
    assert result.usage is not None
    assert result.usage.steps == 1
    assert conversation.branch_calls == ["assistant-1"]


@_run_async
async def test_preflight_runs_on_final_context_before_model_request_intent():
    provider = _Provider([AssistantMessage(content=(TextBlock("done"),))])
    operations = _Operations(_queued_state())
    seen = []

    async def preflight(**kwargs):
        seen.append(kwargs)
        return SimpleNamespace(compaction_required=False)

    with patch(
        "pickel.runtime.operation_driver.preflight_model_context",
        side_effect=preflight,
    ):
        result = await _driver(operations, provider).drive_operation("operation-1")

    assert result.status == "succeeded"
    assert len(seen) == 1
    assert seen[0]["context"] == provider.contexts[0]
    assert seen[0]["provider"] is provider
    assert seen[0]["compaction_threshold"] is None


@_run_async
async def test_preflight_compaction_signal_does_not_create_intent_or_provider_call():
    class _CountingProvider(_Provider):
        def __init__(self):
            super().__init__([AssistantMessage(content=(TextBlock("unused"),))])
            self.count_calls = 0

        async def count_context_tokens(self, context):
            self.count_calls += 1
            return 100

    provider = _CountingProvider()
    package = _loaded_package().version
    package.model_policy = SimpleNamespace(
        primary=SimpleNamespace(
            effective_input_token_limit=lambda: 100,
        )
    )
    operations = _Operations(_queued_state())
    driver = OperationDriver(
        operation_service=operations,
        conversation_service=_Conversation(),
        package_loader=lambda _: package,
        effects_resolver=lambda _: RuntimeEffects(provider=provider),
        model_call_service=_FakeModelCallService(operations),
        model_context_builder=_ContextBuilder(),
    )

    result = await driver.drive_operation("operation-1")

    assert result.status == "failed"
    assert result.state.error.code == "history_compaction_unavailable"
    assert provider.count_calls == 1
    assert provider.calls == 0
    assert operations.state.current_step is None


@_run_async
async def test_unavailable_token_count_falls_back_and_continues_model_request():
    class _UnavailableProvider(_Provider):
        async def count_context_tokens(self, context):
            del context
            return None

    provider = _UnavailableProvider([AssistantMessage(content=(TextBlock("unused"),))])
    operations = _Operations(_queued_state())

    result = await _driver(operations, provider).drive_operation("operation-1")

    assert result.status == "succeeded"
    assert provider.calls == 1


@_run_async
async def test_compaction_is_attempted_once_per_step_when_rebuilt_context_stays_large():
    class _CountingProvider(_Provider):
        def __init__(self):
            super().__init__([AssistantMessage(content=(TextBlock("unused"),))])
            self.count_calls = 0

        async def count_context_tokens(self, context):
            self.count_calls += 1
            return 90

    provider = _CountingProvider()
    package = _loaded_package().version
    package.model_policy = SimpleNamespace(
        primary=SimpleNamespace(effective_input_token_limit=lambda: 90),
        worker=object(),
    )
    operations = _Operations(_queued_state())
    conversation = _CompactionConversation()

    class _Generator:
        async def generate(self, **kwargs):
            compact_calls.append(kwargs)
            return HistoryCompaction("summary", "node-1")

    compact_calls = []
    driver = OperationDriver(
        operation_service=operations,
        conversation_service=conversation,
        package_loader=lambda _: package,
        effects_resolver=lambda _: RuntimeEffects(provider=provider),
        model_call_service=_FakeModelCallService(operations),
        model_context_builder=_ContextBuilder(),
        history_compaction_generator=_Generator(),
    )

    result = await driver.drive_operation("operation-1")

    assert result.status == "failed"
    assert result.state.error.code == "history_compaction_no_progress"
    assert len(compact_calls) == 1
    assert compact_calls[0]["preflight"].token_count == 90
    assert conversation.nodes[-1].content == HistoryCompaction("summary", "node-1")
    assert provider.count_calls == 2
    assert provider.calls == 0


@_run_async
async def test_recovery_after_compaction_node_does_not_invoke_worker_again():
    class _CountingProvider(_Provider):
        def __init__(self):
            super().__init__([AssistantMessage(content=(TextBlock("unused"),))])
            self.count_calls = 0

        async def count_context_tokens(self, context):
            self.count_calls += 1
            return 100

    provider = _CountingProvider()
    package = _loaded_package().version
    package.model_policy = SimpleNamespace(
        primary=SimpleNamespace(effective_input_token_limit=lambda: 100),
        worker=SimpleNamespace(provider="worker", model="worker-model"),
    )
    operations = _Operations(_queued_state())
    driver = OperationDriver(
        operation_service=operations,
        conversation_service=_RecoveredCompactionConversation(),
        package_loader=lambda _: package,
        effects_resolver=lambda _: RuntimeEffects(provider=provider),
        model_call_service=_FakeModelCallService(operations),
        model_context_builder=_ContextBuilder(),
    )
    result = await driver.drive_operation("operation-1")

    assert result.status == "failed"
    assert result.state.error.code == "history_compaction_no_progress"
    assert provider.count_calls == 1
    assert provider.calls == 0


@_run_async
async def test_formal_context_keeps_complete_active_branch_without_turn_window():
    projected = tuple(
        UserMessage((TextBlock(f"message-{index}"),)) for index in range(8)
    )
    seen = []

    class _Builder:
        def build_model_context(self, **kwargs):
            seen.append(tuple(kwargs["visible_messages"]))
            return ModelContext(
                system=SystemContent(), messages=tuple(kwargs["visible_messages"])
            )

    driver = OperationDriver(
        operation_service=_Operations(
            replace(
                _queued_state(),
                status="running",
                current_step=ModelStepState(
                    "step-1", 1, "preparing_request", 0, None, None, ()
                ),
            )
        ),
        conversation_service=_Conversation(),
        package_loader=lambda _: _loaded_package().version,
        effects_resolver=lambda _: RuntimeEffects(provider=object()),
        model_context_builder=_Builder(),
    )

    with patch(
        "pickel.runtime.operation_driver.ConversationProjector.project_conversation_messages",
        return_value=projected,
    ):
        context = await driver._build_context(
            operation=_operation(),
            state=driver._operations.state,
            package=_loaded_package().version,
            effects=RuntimeEffects(provider=object()),
        )

    assert seen == [projected]
    assert context.messages == projected


class _CancellationOperations(_Operations):
    def reconcile_cancellation(self, operation_id, *, reason):
        return ()

    def cancellation_ready(self, operation_id):
        return True

    def commit_transition(self, *, state, expected_revision, node, updated_at=None):
        return super().commit_transition(
            state=state, expected_revision=expected_revision, node=node
        )


@_run_async
async def test_current_cancellation_captures_leaf_before_terminal_commit():
    conversation = _UsageConversation(_usage_nodes(), active_leaf="assistant-1")
    state = replace(
        _queued_state(),
        status="cancelling",
        cancellation=SimpleNamespace(cause="user requested"),
    )
    operations = _CancellationOperations(state)
    driver = OperationDriver(
        operation_service=operations,
        conversation_service=conversation,
        package_loader=lambda _: _loaded_package().version,
        effects_resolver=lambda _: RuntimeEffects(
            provider=_Provider([AssistantMessage(content=(TextBlock("unused"),))])
        ),
        model_context_builder=_ContextBuilder(),
    )

    result = await driver.drive_operation("operation-1")

    assert result.status == "cancelled"
    assert result.usage is not None
    assert result.usage.steps == 1
    assert conversation.branch_calls == ["assistant-1"]


@pytest.mark.parametrize("status", ["failed", "cancelled"])
@_run_async
async def test_historical_failure_or_cancellation_has_no_usage_endpoint(status):
    conversation = _UsageConversation(
        _usage_nodes(), active_leaf="assistant-next-operation"
    )
    state = replace(
        _queued_state(),
        status=status,
        error=(
            AgentRunError(code="test", message="test", retryable=False)
            if status == "failed"
            else None
        ),
        cancellation=(SimpleNamespace(cause="test") if status == "cancelled" else None),
    )
    operations = _Operations(state)
    driver = OperationDriver(
        operation_service=operations,
        conversation_service=conversation,
        package_loader=lambda _: pytest.fail("历史终态不应加载 Package"),
        effects_resolver=lambda _: pytest.fail("历史终态不应解析 Effects"),
    )

    result = await driver.drive_operation("operation-1")

    assert result.usage is None
    assert conversation.branch_calls == []


def _loaded_package(
    *,
    replay_policy="safe",
    input_schema=None,
    output_schema=None,
    tool_name="run",
):
    output_schema = output_schema or {"type": "string"}
    version = SimpleNamespace(
        package_version_id="agentpkg_" + "a" * 64,
        agent_id="Pickle",
        format_version=1,
        behavior_instruction="behavior",
        delegation_policy=AgentDelegationPolicy("Pickle", ("Pickle",)),
        skills=(),
        runtime_policy=AgentRuntimePolicy(
            max_model_steps=8,
            context_turn_window=8,
        ),
        tools=(
            SimpleNamespace(
                name=tool_name,
                description=tool_name,
                replay_policy=replay_policy,
                source=SimpleNamespace(value="builtin"),
                implementation_ref=SimpleNamespace(name=tool_name),
                input_schema=input_schema or {"type": "object"},
                output_schema=output_schema,
            ),
        ),
    )
    return SimpleNamespace(version=version)


def _driver(
    operations,
    provider,
    *,
    tool=None,
    replay_policy="safe",
    tool_name="run",
    invoke_hook=None,
    input_schema=None,
    output_schema=None,
    recall_sources=(),
    model_context_builder=None,
):
    effects = RuntimeEffects(
        provider=provider,
        execute_tool=tool,
        invoke_hook_effect=invoke_hook,
        recall_sources=tuple(recall_sources),
    )
    return OperationDriver(
        operation_service=operations,
        conversation_service=_Conversation(),
        package_loader=lambda package_version_id: _loaded_package(
            replay_policy=replay_policy,
            input_schema=input_schema,
            output_schema=output_schema,
            tool_name=tool_name,
        ).version,
        effects_resolver=lambda package_version_id: effects,
        model_call_service=_FakeModelCallService(operations),
        model_context_builder=model_context_builder or _ContextBuilder(),
        step_id_factory=lambda: "step-1",
        node_id_factory=lambda: "node-result",
    )


def _tool_result(tool_call_id: str, tool_name: str, text: str) -> ToolResultMessage:
    return ToolResultMessage(
        tool_call_id=tool_call_id,
        tool_name=tool_name,
        content=(TextBlock(text),),
    )


def test_delegation_intent_resolves_allowed_child_package_before_effect() -> None:
    parent_tool = SimpleNamespace(name="read")
    parent = SimpleNamespace(
        agent_id="Pickle",
        package_version_id="agentpkg_" + "p" * 64,
        format_version=3,
        delegation_policy=AgentDelegationPolicy("Worker", ("Pickle", "Worker")),
        tools=(parent_tool,),
    )
    child = SimpleNamespace(
        agent_id="Worker",
        package_version_id="agentpkg_" + "c" * 64,
        format_version=3,
        tools=(SimpleNamespace(name="read"),),
    )
    driver = _driver(
        _Operations(_queued_state()),
        _Provider([]),
    )
    driver._delegation_package_resolver = lambda _operation, _parent, target: child
    call = SimpleNamespace(arguments={"agent_id": "Worker"})

    intent = driver._resolve_delegation_intent(
        operation=_operation(), package=parent, call=call
    )

    assert intent == DelegateAgentIntent(child.package_version_id)


def test_historical_delegation_is_exact_same_package_without_resolver() -> None:
    package = SimpleNamespace(
        agent_id="Pickle",
        format_version=2,
        package_version_id="agentpkg_" + "h" * 64,
        delegation_policy=AgentDelegationPolicy("Pickle", ("Pickle",)),
        tools=(),
    )
    driver = _driver(_Operations(_queued_state()), _Provider([]))
    call = SimpleNamespace(arguments={})

    intent = driver._resolve_delegation_intent(
        operation=_operation(), package=package, call=call
    )

    assert intent == DelegateAgentIntent(package.package_version_id)


def test_format3_delegation_requires_package_resolver() -> None:
    package = SimpleNamespace(
        agent_id="Pickle",
        format_version=3,
        package_version_id="agentpkg_" + "f" * 64,
        delegation_policy=AgentDelegationPolicy("Pickle", ("Pickle",)),
        tools=(),
    )
    driver = _driver(_Operations(_queued_state()), _Provider([]))

    with pytest.raises(ValueError, match="必须提供 child Package 解析器"):
        driver._resolve_delegation_intent(
            operation=_operation(), package=package, call=SimpleNamespace(arguments={})
        )


@_run_async
async def test_request_intent_and_assistant_are_committed_as_separate_atomic_facts():
    provider = _Provider([AssistantMessage(content=(TextBlock(text="done"),))])
    operations = _Operations(_queued_state())
    result = await _driver(operations, provider).drive_operation("operation-1")

    assert result.status == "succeeded"
    request_states = [
        state
        for state, node in operations.transition_calls
        if state.current_step is not None
        and state.current_step.phase == "request_ready"
    ]
    assert request_states
    assert request_states[0].current_step.request_intent is not None
    assistant_commits = [
        item for item in operations.transition_calls if item[1] is not None
    ]
    assert len(assistant_commits) == 1
    assert assistant_commits[0][1].node_id == "node-result"
    # Intent 先以 attempt=0 冻结；发起 Provider 前再单独 CAS 到 attempt=1。
    assert [item.current_step.request_attempt for item in request_states] == [0, 1]


@_run_async
async def test_retryable_provider_failure_reuses_intent_and_counts_real_attempts():
    provider = _Provider(
        [
            TimeoutError("first"),
            TimeoutError("second"),
            AssistantMessage(content=(TextBlock(text="done"),)),
        ]
    )
    operations = _Operations(_queued_state())
    delays: list[float] = []
    driver = _driver(operations, provider)
    driver._sleep = lambda delay: _record_delay(delays, delay)

    result = await driver.drive_operation("operation-1")

    assert result.status == "succeeded"
    assert provider.calls == 3
    assert delays == [20.0, 60.0]
    attempts = [
        state.current_step.request_attempt
        for state, _ in operations.transition_calls
        if state.current_step is not None
        and state.current_step.phase == "request_ready"
    ]
    assert attempts == [0, 1, 2, 3]
    assert provider.contexts[0] is provider.contexts[1] is provider.contexts[2]


@_run_async
async def test_provider_failure_exhaustion_commits_failed_terminal_state():
    provider = _Provider([TimeoutError("1"), TimeoutError("2"), TimeoutError("3")])
    operations = _Operations(_queued_state())
    driver = _driver(operations, provider)
    driver._sleep = lambda delay: _record_delay([], delay)

    result = await driver.drive_operation("operation-1")

    assert result.status == "failed"
    assert result.state.current_step is None
    assert result.state.error == AgentRunError(
        code="provider_timeout",
        message="模型请求超时",
        retryable=True,
    )
    assert provider.calls == 3


@_run_async
async def test_provider_failure_after_first_output_does_not_retry():
    provider = _PartialOutputFailureProvider([])
    operations = _Operations(_queued_state())
    driver = _driver(operations, provider)
    delays: list[float] = []
    driver._sleep = lambda delay: _record_delay(delays, delay)

    result = await driver.drive_operation("operation-1")

    assert result.status == "failed"
    assert result.state.error is not None
    assert result.state.error.code == "provider_timeout"
    assert provider.calls == 1
    assert delays == []


@_run_async
async def test_invalid_provider_response_fails_without_retry():
    provider = _Provider([ValueError("bad wire")])
    operations = _Operations(_queued_state())

    result = await _driver(operations, provider).drive_operation("operation-1")

    assert result.status == "failed"
    assert result.state.error is not None
    assert result.state.error.code == "provider_response_invalid"
    assert result.state.error.retryable is False
    assert provider.calls == 1


async def _record_delay(target: list[float], delay: float) -> None:
    target.append(delay)


@_run_async
async def test_first_step_claims_pending_steer_and_inject_before_context():
    pending = (
        SimpleNamespace(message_id="steer-1"),
        SimpleNamespace(message_id="inject-1"),
    )
    operations = _Operations(
        _queued_state(),
        pending=pending,
    )
    provider = _Provider([AssistantMessage(content=(TextBlock(text="done"),))])

    result = await _driver(operations, provider).drive_operation("operation-1")

    assert result.status == "succeeded"
    assert [call[0] for call in operations.claim_calls] == [("steer-1", "inject-1")]
    assert operations.claim_calls[0][1].current_step.phase == "preparing_request"


def _stopping_state_for_driver() -> AgentRunState:
    return AgentRunState(
        operation_id="operation-1",
        revision=3,
        status="running",
        waiting_reason=None,
        completed_step_count=0,
        current_step=ModelStepState(
            "step-1", 1, "awaiting_tools", 0, None, "node-result", ()
        ),
        final_assistant_node_id=None,
        error=None,
        cancellation=None,
    )


class _StoppingOperations(_Operations):
    def __init__(self, state, *, continue_commit=True):
        super().__init__(state, pending=(SimpleNamespace(message_id="steer-1"),))
        self.continue_commit = continue_commit
        self.terminal_attempts = 0

    def commit_transition(self, *, state, expected_revision, node):
        if state.status == "succeeded":
            self.terminal_attempts += 1
            if self.terminal_attempts == 1:
                return False
        if state.current_step is None and state.status == "running":
            if not self.continue_commit:
                return False
        return super().commit_transition(
            state=state, expected_revision=expected_revision, node=node
        )


class _DisappearingStoppingOperations(_StoppingOperations):
    def list_pending_step_messages(self, *, session_id):
        pending = super().list_pending_step_messages(session_id=session_id)
        self.pending = ()
        return pending

    def commit_transition(self, *, state, expected_revision, node):
        if state.current_step is None and state.status == "running":
            return False
        return super().commit_transition(
            state=state, expected_revision=expected_revision, node=node
        )


@_run_async
async def test_stopping_terminal_guard_retries_as_continue_then_claims_next_step():
    operations = _StoppingOperations(_stopping_state_for_driver())
    provider = _Provider([AssistantMessage(content=(TextBlock(text="done"),))])

    result = await _driver(operations, provider).drive_operation("operation-1")

    assert result.status == "succeeded"
    assert any(state.status == "succeeded" for state, _ in operations.transition_calls)
    assert any(
        state.status == "running" and state.current_step is None
        for state, _ in operations.transition_calls
    )


@_run_async
async def test_stopping_continue_guard_conflict_stops_without_provider():
    operations = _StoppingOperations(
        _stopping_state_for_driver(), continue_commit=False
    )
    provider = _Provider([AssistantMessage(content=(TextBlock(text="must-not-run"),))])

    with pytest.raises(RuntimeError, match="CAS"):
        await _driver(operations, provider).drive_operation("operation-1")
    assert provider.calls == 0


@_run_async
async def test_stopping_message_disappearing_after_guard_stops_without_provider():
    operations = _DisappearingStoppingOperations(_stopping_state_for_driver())
    provider = _Provider([AssistantMessage(content=(TextBlock(text="must-not-run"),))])

    with pytest.raises(RuntimeError, match="CAS"):
        await _driver(operations, provider).drive_operation("operation-1")
    assert provider.calls == 0


@_run_async
async def test_tool_replay_policy_and_intent_before_effect():
    tool_message = AssistantMessage(
        content=(ToolCallBlock(id="tool-1", name="run", arguments={}),)
    )
    provider = _Provider(
        [tool_message, AssistantMessage(content=(TextBlock(text="ok"),))]
    )
    seen = []

    async def execute_tool(*, operation, state, tool_call_id, host_calls):
        call = next(
            c for c in state.current_step.tool_calls if c.tool_call_id == tool_call_id
        )
        seen.append((call.status, call.replay_policy))
        return _tool_result(tool_call_id, "run", "ok")

    operations = _Operations(_queued_state())
    events = []
    result = await _driver(
        operations, provider, tool=execute_tool, replay_policy="never"
    ).drive_operation("operation-1", consume_tool_event=events.append)

    assert result.status == "succeeded"
    assert seen == [("intent_recorded", "never")]
    assert [type(event) for event in events] == [ToolCallStarted, ToolCallCompleted]
    assert events[0].tool_name == "run"
    assert events[0].envelope.identity.tool_call_id == "tool-1"
    assert events[1].content == "ok"


@_run_async
async def test_delegate_agent_freezes_parent_package_before_effect():
    tool_message = AssistantMessage(
        content=(
            ToolCallBlock(
                id="tool-1",
                name="delegate_agent",
                arguments={"description": "child", "prompt": "work"},
            ),
        )
    )
    seen = []

    async def execute_tool(*, operation, state, tool_call_id, host_calls):
        call = next(
            call
            for call in state.current_step.tool_calls
            if call.tool_call_id == tool_call_id
        )
        seen.append((call.status, call.execution_intent))
        return _tool_result(tool_call_id, "delegate_agent", "accepted")

    operations = _Operations(_queued_state())
    result = await _driver(
        operations,
        _Provider([tool_message, AssistantMessage(content=(TextBlock(text="done"),))]),
        tool=execute_tool,
        tool_name="delegate_agent",
        replay_policy="safe",
    ).drive_operation("operation-1")

    assert result.status == "succeeded"
    assert seen == [
        (
            "intent_recorded",
            DelegateAgentIntent(_operation().agent_package_version_id),
        )
    ]


@_run_async
async def test_delegate_agent_safe_replay_uses_persisted_intent():
    call = ToolCallState(
        tool_call_id="tool-1",
        tool_name="delegate_agent",
        arguments={"description": "child", "prompt": "work"},
        status="intent_recorded",
        approval=None,
        replay_policy="safe",
        execution_intent=DelegateAgentIntent(_operation().agent_package_version_id),
        decision_reason=None,
        result_node_id=None,
        is_error=None,
    )
    state = replace(
        _queued_state(),
        status="running",
        current_step=ModelStepState(
            step_id="step-1",
            step_sequence=1,
            phase="awaiting_tools",
            request_attempt=1,
            request_intent=None,
            assistant_message_node_id="assistant-1",
            tool_calls=(call,),
        ),
    )
    seen = []

    async def execute_tool(*, operation, state, tool_call_id, host_calls):
        persisted = next(
            item
            for item in state.current_step.tool_calls
            if item.tool_call_id == tool_call_id
        )
        seen.append(persisted.execution_intent)
        return _tool_result(tool_call_id, "delegate_agent", "accepted")

    operations = _Operations(state)
    result = await _driver(
        operations,
        _Provider([AssistantMessage(content=(TextBlock(text="done"),))]),
        tool=execute_tool,
        tool_name="delegate_agent",
        replay_policy="safe",
    ).drive_operation("operation-1")

    assert result.status == "succeeded"
    assert seen == [DelegateAgentIntent(_operation().agent_package_version_id)]


@_run_async
async def test_send_message_is_safe_replay_without_a_new_intent_type():
    tool_message = AssistantMessage(
        content=(
            ToolCallBlock(
                id="tool-1",
                name="send_message",
                arguments={"child_session_id": "child-1", "message": "continue"},
            ),
        )
    )
    seen = []

    async def execute_tool(*, operation, state, tool_call_id, host_calls):
        call = next(
            item
            for item in state.current_step.tool_calls
            if item.tool_call_id == tool_call_id
        )
        seen.append((call.status, call.replay_policy, call.execution_intent))
        return _tool_result(tool_call_id, "send_message", "accepted")

    result = await _driver(
        _Operations(_queued_state()),
        _Provider([tool_message, AssistantMessage(content=(TextBlock("done"),))]),
        tool=execute_tool,
        tool_name="send_message",
        replay_policy="safe",
    ).drive_operation("operation-1")

    assert result.status == "succeeded"
    assert seen == [("intent_recorded", "safe", None)]


@_run_async
async def test_list_agents_is_safe_replay_without_a_new_intent_type():
    tool_message = AssistantMessage(
        content=(ToolCallBlock(id="tool-1", name="list_agents", arguments={}),)
    )
    seen = []

    async def execute_tool(*, operation, state, tool_call_id, host_calls):
        call = next(
            item
            for item in state.current_step.tool_calls
            if item.tool_call_id == tool_call_id
        )
        seen.append((call.status, call.replay_policy, call.execution_intent))
        return _tool_result(tool_call_id, "list_agents", "accepted")

    result = await _driver(
        _Operations(_queued_state()),
        _Provider([tool_message, AssistantMessage(content=(TextBlock("done"),))]),
        tool=execute_tool,
        tool_name="list_agents",
        replay_policy="safe",
    ).drive_operation("operation-1")

    assert result.status == "succeeded"
    assert seen == [("intent_recorded", "safe", None)]


@_run_async
async def test_report_is_safe_replay_without_a_new_intent_type():
    tool_message = AssistantMessage(
        content=(
            ToolCallBlock(id="tool-1", name="report", arguments={"output": "finding"}),
        )
    )
    seen = []

    async def execute_tool(*, operation, state, tool_call_id, host_calls):
        call = next(
            item
            for item in state.current_step.tool_calls
            if item.tool_call_id == tool_call_id
        )
        seen.append((call.status, call.replay_policy, call.execution_intent))
        return _tool_result(tool_call_id, "report", "accepted")

    result = await _driver(
        _Operations(_queued_state()),
        _Provider([tool_message, AssistantMessage(content=(TextBlock("done"),))]),
        tool=execute_tool,
        tool_name="report",
        replay_policy="safe",
    ).drive_operation("operation-1")

    assert result.status == "succeeded"
    assert seen == [("intent_recorded", "safe", None)]


@_run_async
async def test_pre_tool_ask_freezes_updated_arguments_and_waits_for_approval():
    tool_message = AssistantMessage(
        content=(ToolCallBlock(id="tool-1", name="run", arguments={"old": True}),)
    )

    async def invoke_hook(name, event):
        if name == "before_request":
            return ContextContributions()
        assert name == "pre_tool_use"
        assert event.identity.operation_id == "operation-1"
        assert event.identity.step_id == "step-1"
        return PreToolUseDecision(
            action="ask",
            updated_arguments={"approved_input": True},
            reason="需要确认",
        )

    operations = _Operations(_queued_state())
    result = await _driver(
        operations,
        _Provider([tool_message]),
        invoke_hook=invoke_hook,
    ).drive_operation("operation-1")

    assert result.status == "waiting"
    assert result.state.waiting_reason == "tool_approval"
    call = result.state.current_step.tool_calls[0]
    assert call.status == "waiting_approval"
    assert dict(call.arguments) == {"approved_input": True}
    assert call.approval is not None
    assert call.approval.requested_by == "hook"
    assert call.approval.reason == "需要确认"


@_run_async
async def test_pre_tool_hook_failure_converges_model_call_and_operation():
    tool_message = AssistantMessage(
        content=(ToolCallBlock(id="tool-1", name="run", arguments={}),)
    )

    async def invoke_hook(name, _event):
        if name == "before_request":
            return ContextContributions()
        raise RuntimeError("hook exploded")

    operations = _Operations(_queued_state())
    driver = _driver(
        operations,
        _Provider([tool_message]),
        invoke_hook=invoke_hook,
    )
    result = await driver.drive_operation("operation-1")

    assert result.status == "failed"
    assert result.state.status == "failed"
    assert result.state.final_assistant_node_id is None
    assert driver._model_calls.calls
    assert all(
        call.status == "completed" for call in driver._model_calls.calls.values()
    )


@_run_async
async def test_pre_tool_deny_becomes_ordered_error_without_executing_tool():
    tool_message = AssistantMessage(
        content=(
            ToolCallBlock(id="tool-1", name="run", arguments={}),
            ToolCallBlock(id="tool-2", name="run", arguments={}),
        )
    )
    executed = []

    async def invoke_hook(_name, event):
        if _name == "before_request":
            return ContextContributions()
        return PreToolUseDecision(
            action="deny" if event.identity.tool_call_id == "tool-1" else "allow",
            reason=("策略拒绝" if event.identity.tool_call_id == "tool-1" else None),
        )

    async def execute_tool(*, operation, state, tool_call_id, host_calls):
        executed.append(tool_call_id)
        return _tool_result(tool_call_id, "run", "ok")

    operations = _Operations(_queued_state())
    result = await _driver(
        operations,
        _Provider([tool_message, AssistantMessage(content=(TextBlock(text="done"),))]),
        tool=execute_tool,
        invoke_hook=invoke_hook,
    ).drive_operation("operation-1")

    assert result.status == "succeeded"
    assert executed == ["tool-2"]
    tool_results = [
        node.content
        for _, node in operations.transition_calls
        if node is not None
        and node.content_type == "agent_message"
        and hasattr(node.content, "tool_call_id")
    ]
    assert [message.tool_call_id for message in tool_results] == ["tool-1", "tool-2"]
    assert tool_results[0].is_error is True
    assert tool_results[0].content[0].text == "工具调用被拒绝：策略拒绝"


@_run_async
async def test_before_request_contributions_are_frozen_in_request_intent():
    class Recall:
        async def provide(self, *, session_id, current_user_text):
            assert session_id == "session-1"
            return [UserMessage((TextBlock("recall"),))]

    hook_calls = []

    async def invoke_hook(name, event):
        if name != "before_request":
            return None
        hook_calls.append(event)
        assert event.identity.session_id == "session-1"
        assert event.identity.operation_id == "operation-1"
        assert event.identity.step_id == "step-1"
        assert event.identity.step_sequence == 1
        assert event.visible_messages == ()
        assert event.recall_messages[0].content[0].text == "recall"
        return ContextContributions(
            system_sections=(SystemSection("request_hook", "hook system"),),
            messages=(UserMessage((TextBlock("hook message"),)),),
        )

    provider = _Provider([AssistantMessage(content=(TextBlock(text="done"),))])
    operations = _Operations(_queued_state())
    result = await _driver(
        operations,
        provider,
        invoke_hook=invoke_hook,
        recall_sources=(Recall(),),
        model_context_builder=ModelContextBuilder(),
    ).drive_operation("operation-1")

    assert result.status == "succeeded"
    assert len(hook_calls) == 1
    intent_states = [
        state
        for state, _node in operations.transition_calls
        if state.current_step is not None
        and state.current_step.request_intent is not None
    ]
    intent_context = intent_states[0].current_step.request_intent.model_context
    assert intent_context == provider.contexts[0]
    assert [section.name for section in intent_context.system.sections] == [
        "behavior",
        "multi_agent_guidance",
        "request_hook",
    ]
    assert [message.content[0].text for message in intent_context.messages] == [
        "recall",
        "hook message",
    ]


@_run_async
async def test_unknown_tool_is_rejected_before_intent_or_execution():
    tool_message = AssistantMessage(
        content=(ToolCallBlock(id="tool-1", name="missing", arguments={}),)
    )
    executed = []

    async def execute_tool(*, operation, state, tool_call_id, host_calls):
        executed.append(tool_call_id)
        return _tool_result(tool_call_id, "missing", "must not run")

    operations = _Operations(_queued_state())
    result = await _driver(
        operations,
        _Provider([tool_message, AssistantMessage(content=(TextBlock(text="done"),))]),
        tool=execute_tool,
    ).drive_operation("operation-1")

    assert result.status == "succeeded"
    assert executed == []
    rejected_states = [
        state
        for state, _node in operations.transition_calls
        if state.current_step is not None
        and any(call.status == "rejected" for call in state.current_step.tool_calls)
    ]
    assert rejected_states
    call = rejected_states[0].current_step.tool_calls[0]
    assert call.execution_intent is None
    assert call.decision_reason == "工具不可用: missing"


@_run_async
async def test_updated_arguments_are_validated_before_intent():
    tool_message = AssistantMessage(
        content=(ToolCallBlock(id="tool-1", name="run", arguments={"id": 1}),)
    )
    executed = []

    async def invoke_hook(_name, _event):
        return PreToolUseDecision(updated_arguments={"id": "invalid"})

    async def execute_tool(*, operation, state, tool_call_id, host_calls):
        executed.append(tool_call_id)
        return _tool_result(tool_call_id, "run", "must not run")

    operations = _Operations(_queued_state())
    result = await _driver(
        operations,
        _Provider([tool_message, AssistantMessage(content=(TextBlock(text="done"),))]),
        tool=execute_tool,
        invoke_hook=invoke_hook,
        input_schema={
            "type": "object",
            "properties": {"id": {"type": "integer"}},
            "required": ["id"],
        },
    ).drive_operation("operation-1")

    assert result.status == "succeeded"
    assert executed == []
    rejected = next(
        call
        for state, _node in operations.transition_calls
        if state.current_step is not None
        for call in state.current_step.tool_calls
        if call.status == "rejected"
    )
    assert dict(rejected.arguments) == {"id": "invalid"}
    assert rejected.decision_reason.startswith("工具参数无效: $.id")


@_run_async
async def test_safe_and_never_tool_intent_recovery_have_different_outcomes():
    for replay_policy, expected in (("safe", "executed"), ("never", "waiting")):
        call = ToolCallState(
            tool_call_id="tool-1",
            tool_name="run",
            arguments={},
            status="intent_recorded",
            approval=None,
            replay_policy=replay_policy,
            execution_intent=None,
            decision_reason=None,
            result_node_id=None,
            is_error=None,
        )
        state = replace(
            _queued_state(),
            status="running",
            current_step=ModelStepState(
                step_id="step-1",
                step_sequence=1,
                phase="awaiting_tools",
                request_attempt=1,
                request_intent=None,
                assistant_message_node_id="assistant-1",
                tool_calls=(call,),
            ),
        )
        executed = []

        async def execute_tool(*, operation, state, tool_call_id, host_calls):
            executed.append(tool_call_id)
            return _tool_result(tool_call_id, "run", "ok")

        operations = _Operations(state)
        result = await _driver(
            operations,
            _Provider([AssistantMessage(content=(TextBlock(text="ok"),))]),
            tool=execute_tool,
            replay_policy=replay_policy,
        ).drive_operation("operation-1")
        assert ("executed" if executed else result.status) == expected


@_run_async
async def test_rejected_tool_call_becomes_error_result_in_provider_order():
    rejected = ToolCallState(
        tool_call_id="tool-1",
        tool_name="run",
        arguments={},
        status="rejected",
        approval=ToolApproval(
            requested_at=datetime.now(timezone.utc),
            requested_by="tool_policy",
            reason="需要用户确认",
            decision=ToolApprovalDecision(
                outcome="denied",
                decided_at=datetime.now(timezone.utc),
                actor_id="user-1",
                reason="风险过高",
            ),
        ),
        replay_policy="never",
        execution_intent=None,
        decision_reason=None,
        result_node_id=None,
        is_error=None,
    )
    ready = replace(
        rejected,
        tool_call_id="tool-2",
        status="ready",
        approval=None,
        replay_policy="safe",
    )
    state = replace(
        _queued_state(),
        status="running",
        current_step=ModelStepState(
            step_id="step-1",
            step_sequence=1,
            phase="awaiting_tools",
            request_attempt=1,
            request_intent=None,
            assistant_message_node_id="assistant-1",
            tool_calls=(rejected, ready),
        ),
    )
    executed = []

    async def execute_tool(*, operation, state, tool_call_id, host_calls):
        executed.append(tool_call_id)
        return _tool_result(tool_call_id, "run", "ok")

    operations = _Operations(state)
    result = await _driver(
        operations,
        _Provider([AssistantMessage(content=(TextBlock(text="done"),))]),
        tool=execute_tool,
    ).drive_operation("operation-1")

    assert result.status == "succeeded"
    assert executed == ["tool-2"]
    tool_results = [
        node.content
        for _, node in operations.transition_calls
        if node is not None
        and node.content_type == "agent_message"
        and hasattr(node.content, "tool_call_id")
    ]
    assert [message.tool_call_id for message in tool_results] == ["tool-1", "tool-2"]
    assert tool_results[0].is_error is True
    assert tool_results[0].content[0].text == "工具调用被拒绝：风险过高"


@_run_async
async def test_cas_false_stops_before_provider_effect():
    provider = _Provider([AssistantMessage(content=(TextBlock(text="must-not-run"),))])
    operations = _Operations(_queued_state(), commit_transition=False, cas=False)
    with pytest.raises(RuntimeError, match="CAS"):
        await _driver(operations, provider).drive_operation("operation-1")
    assert provider.calls == 0


@_run_async
async def test_package_load_failure_is_persisted_as_retryable_failed_state():
    operations = _Operations(_queued_state())
    driver = OperationDriver(
        operation_service=operations,
        conversation_service=_Conversation(),
        package_loader=lambda _: (_ for _ in ()).throw(
            PackageLoadError(
                "tool_unavailable", _operation().agent_package_version_id, "missing"
            )
        ),
        effects_resolver=lambda _: pytest.fail("Package 失败后不能解析 Effects"),
        model_context_builder=_ContextBuilder(),
    )

    result = await driver.drive_operation("operation-1")

    assert result.status == "failed"
    assert result.state.error is not None
    assert result.state.error.code == "tool_unavailable"
    assert result.state.error.retryable is True
