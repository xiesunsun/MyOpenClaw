"""OperationDriver v10 的最小恢复与副作用合同。"""

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import asyncio
from functools import wraps

import pytest

from pickel.context.model_context import ModelContext, SystemContent
from pickel.conversations.agent_message import AssistantMessage
from pickel.conversations.content_blocks import TextBlock, ToolCallBlock
from pickel.operations.agent_run_state import (
    AgentRunState,
    ModelStepState,
    ToolApproval,
    ToolApprovalDecision,
    ToolCallState,
)
from pickel.operations.session_operation import SessionOperation
from pickel.providers.stream import StreamCompleted
from pickel.runtime.operation_driver import OperationDriver
from pickel.runtime.runtime_effects import RuntimeEffects
from pickel.agents.agent_package_loader import PackageLoadError
from pickel.workspaces.workspace_binding import WorkspaceBinding


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


class _ContextBuilder:
    def build_model_context(self, **kwargs):
        return ModelContext(
            system=SystemContent(), messages=tuple(kwargs["visible_messages"])
        )


class _Provider:
    def __init__(self, messages):
        self.messages = list(messages)
        self.calls = 0

    async def stream(self, context):
        self.calls += 1
        yield StreamCompleted(self.messages.pop(0))


class _Operations:
    def __init__(self, state, *, commit_transition=True, cas=True):
        self.state = state
        self.transition_calls = []
        self.cas = cas
        self.commit_transition_enabled = commit_transition

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


def _loaded_package(*, replay_policy="safe"):
    version = SimpleNamespace(
        package_version_id="agentpkg_" + "a" * 64,
        runtime_policy=SimpleNamespace(max_model_steps=8, context_turn_window=8),
        tools=(SimpleNamespace(name="run", replay_policy=replay_policy),),
    )
    return SimpleNamespace(version=version)


def _driver(operations, provider, *, tool=None, replay_policy="safe"):
    effects = RuntimeEffects(provider=provider, execute_tool=tool)
    return OperationDriver(
        operation_service=operations,
        conversation_service=_Conversation(),
        package_loader=lambda package_version_id: _loaded_package(
            replay_policy=replay_policy
        ).version,
        effects_resolver=lambda package_version_id: effects,
        model_context_builder=_ContextBuilder(),
        step_id_factory=lambda: "step-1",
        node_id_factory=lambda: "node-result",
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
        return SimpleNamespace(
            content="ok", content_blocks=[], is_error=False, structured_content=None
        )

    operations = _Operations(_queued_state())
    result = await _driver(
        operations, provider, tool=execute_tool, replay_policy="never"
    ).drive_operation("operation-1")

    assert result.status == "succeeded"
    assert seen == [("intent_recorded", "never")]


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
            return SimpleNamespace(
                content="ok", content_blocks=[], is_error=False, structured_content=None
            )

        operations = _Operations(state)
        result = await _driver(
            operations,
            _Provider([AssistantMessage(content=(TextBlock(text="ok"),))]),
            tool=execute_tool,
            replay_policy=replay_policy,
        ).drive_operation("operation-1")
        assert ("executed" if executed else result.status) == expected


@_run_async
async def test_denied_tool_call_becomes_error_result_in_provider_order():
    denied = ToolCallState(
        tool_call_id="tool-1",
        tool_name="run",
        arguments={},
        status="denied",
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
        denied,
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
            tool_calls=(denied, ready),
        ),
    )
    executed = []

    async def execute_tool(*, operation, state, tool_call_id, host_calls):
        executed.append(tool_call_id)
        return SimpleNamespace(
            content="ok", content_blocks=[], is_error=False, structured_content=None
        )

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
    assert tool_results[0].content[0].text == "工具调用未获批准：风险过高"


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
