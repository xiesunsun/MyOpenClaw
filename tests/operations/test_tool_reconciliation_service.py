"""ToolExecutionIntent 恢复核对服务的 revision CAS 合同。"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from pickel.operations.agent_run_state import (
    AgentRunState,
    Cancellation,
    ModelStepState,
    ToolCallState,
)
from pickel.operations.tool_reconciliation_service import ToolReconciliationService
from pickel.persistence.errors import StorageConflictError
from pickel.operations.agent_run_state_machine import AgentRunStateMachine
from pickel.tools.base import BaseTool, ToolSpec
from pickel.tools.base import ToolExecutionContext
from pickel.conversations.content_blocks import ToolResultContent

NOW = datetime(2026, 8, 25, tzinfo=timezone.utc)


class _EchoTool(BaseTool):
    spec = ToolSpec(
        name="echo",
        description="echo",
        input_schema={"type": "object"},
        output_schema={"type": "string"},
    )

    async def execute(self, arguments: dict, context: ToolExecutionContext) -> str:
        return ""

    def render(self, validated_value: str) -> tuple[ToolResultContent, ...]:
        from pickel.conversations.content_blocks import TextBlock

        return (TextBlock(validated_value),)


class _NullTool(BaseTool):
    spec = ToolSpec(
        name="echo",
        description="echo",
        input_schema={"type": "object"},
        output_schema={"type": "null"},
    )

    async def execute(self, arguments: dict, context: ToolExecutionContext):
        return None


class _Operations:
    def __init__(self, state: AgentRunState, *, commit: bool = True) -> None:
        self.state = state
        self.commit_enabled = commit
        self.commit_count = 0
        self.nodes = []
        self._state_machine = AgentRunStateMachine()

    def load_agent_run_state(self, _operation_id: str) -> AgentRunState:
        return self.state

    def load_operation(self, _operation_id: str):
        return SimpleNamespace(session_id="session-1")

    def commit_transition(
        self, *, state: AgentRunState, expected_revision: int, node, updated_at
    ) -> bool:
        self.commit_count += 1
        if not self.commit_enabled or expected_revision != self.state.revision:
            return False
        self._state_machine.validate_transition(current=self.state, next_state=state)
        self.state = state
        if node is not None:
            self.nodes.append(node)
        return True


class _Conversations:
    def load_conversation_session(self, _session_id: str):
        return SimpleNamespace(active_node_id="assistant-1")


def _call(
    tool_call_id: str,
    *,
    status: str = "intent_recorded",
    replay_policy: str = "safe",
) -> ToolCallState:
    return ToolCallState(
        tool_call_id=tool_call_id,
        tool_name="echo",
        arguments={"value": tool_call_id},
        status=status,  # type: ignore[arg-type]
        approval=None,
        replay_policy=replay_policy,  # type: ignore[arg-type]
        execution_intent=None,
        decision_reason=None,
        result_node_id=None,
        is_error=None,
    )


def _state(
    *calls: ToolCallState,
    status: str = "waiting",
    revision: int = 7,
) -> AgentRunState:
    return AgentRunState(
        operation_id="operation-1",
        revision=revision,
        status=status,  # type: ignore[arg-type]
        waiting_reason=("tool_reconciliation" if status == "waiting" else None),
        completed_step_count=0,
        current_step=ModelStepState(
            step_id="step-1",
            step_sequence=1,
            phase="awaiting_tools",
            request_attempt=0,
            request_intent=None,
            assistant_message_node_id="assistant-1",
            tool_calls=tuple(calls),
        ),
        final_assistant_node_id=None,
        error=None,
        cancellation=(Cancellation("user", NOW) if status == "cancelling" else None),
    )


def _service(
    *calls: ToolCallState,
    status: str = "waiting",
    revision: int = 7,
    commit: bool = True,
    wake=None,
    tool=None,
):
    operations = _Operations(
        _state(*calls, status=status, revision=revision), commit=commit
    )
    service = ToolReconciliationService(
        operations,
        _Conversations(),
        wake=wake or (lambda _session_id: None),
        resolve_tool=lambda _operation, _call: tool or _EchoTool(),
        node_id_factory=lambda: "tool-result-1",
        now=lambda: NOW,
    )
    return service, operations


_UNSET = object()


def _reconcile(service, *, outcome="completed", result=_UNSET, **kwargs):
    arguments = {
        "operation_id": "operation-1",
        "step_id": "step-1",
        "tool_call_id": "tool-1",
        "expected_revision": 7,
        "outcome": outcome,
    }
    if result is not _UNSET:
        arguments["result"] = result
    arguments.update(kwargs)
    return service.reconcile_tool_call(**arguments)


def test_completed_appends_tool_result_and_resumes_waiting_operation() -> None:
    woken: list[str] = []
    service, operations = _service(_call("tool-1"), wake=woken.append)

    state = _reconcile(
        service,
        result="done",
    )

    assert state.status == "running"
    assert state.waiting_reason is None
    assert state.revision == 8
    assert state.current_step.tool_calls[0].status == "completed"
    assert state.current_step.tool_calls[0].result_node_id == "tool-result-1"
    assert len(operations.nodes) == 1
    node = operations.nodes[0]
    assert node.parent_node_id == "assistant-1"
    assert node.content.tool_call_id == "tool-1"
    assert node.content.content[0].text == "done"
    assert woken == ["session-1"]


def test_completed_accepts_json_null_when_schema_allows_it() -> None:
    service, operations = _service(_call("tool-1"), tool=_NullTool())

    state = _reconcile(service, result=None)

    assert state.status == "running"
    assert operations.nodes[0].content.content[0].text == "null"


def test_not_started_safe_keeps_intent_for_replay_and_wakes() -> None:
    woken: list[str] = []
    service, operations = _service(_call("tool-1"), wake=woken.append)

    state = _reconcile(service, outcome="not_started")

    assert state.status == "running"
    assert state.current_step.tool_calls[0].status == "intent_recorded"
    assert operations.commit_count == 1
    assert woken == ["session-1"]


def test_not_started_never_and_unknown_are_no_ops() -> None:
    for outcome, replay_policy in (("not_started", "never"), ("unknown", "safe")):
        service, operations = _service(_call("tool-1", replay_policy=replay_policy))
        original = operations.state

        state = _reconcile(service, outcome=outcome)

        assert state == original
        assert operations.commit_count == 0


def test_completed_while_cancelling_persists_result_then_wakes_driver() -> None:
    woken: list[str] = []
    service, operations = _service(
        _call("tool-1"), status="cancelling", wake=woken.append
    )

    state = _reconcile(service, result="late result")

    assert state.status == "cancelling"
    assert state.current_step.tool_calls[0].status == "completed"
    assert state.current_step.tool_calls[0].result_node_id == "tool-result-1"
    assert state.cancellation is not None
    assert len(operations.nodes) == 1
    assert operations.nodes[0].content.content[0].text == "late result"
    assert woken == ["session-1"]


def test_not_started_while_cancelling_enters_cancelled_without_fake_result() -> None:
    service, operations = _service(_call("tool-1"), status="cancelling")

    state = _reconcile(service, outcome="not_started")

    assert state.status == "cancelled"
    assert state.current_step is None
    assert operations.nodes == []


def test_stale_revision_is_rejected_without_retry() -> None:
    service, operations = _service(_call("tool-1"))

    with pytest.raises(StorageConflictError, match="expected_revision"):
        _reconcile(
            service,
            expected_revision=6,
            result="done",
        )

    assert operations.commit_count == 0


def test_wrong_step_and_provider_order_are_rejected() -> None:
    service, operations = _service(_call("tool-1"), _call("tool-2"))

    with pytest.raises(StorageConflictError, match="step_id"):
        _reconcile(
            service,
            step_id="old-step",
            result="done",
        )
    with pytest.raises(StorageConflictError, match="Provider ToolCall 顺序"):
        _reconcile(
            service,
            tool_call_id="tool-2",
            result="done",
        )

    assert operations.commit_count == 0


def test_cas_failure_is_not_retried_or_woken() -> None:
    woken: list[str] = []
    service, operations = _service(_call("tool-1"), commit=False, wake=woken.append)

    with pytest.raises(StorageConflictError, match="CAS 失败"):
        _reconcile(service, result="done")

    assert operations.commit_count == 1
    assert operations.state.revision == 7
    assert woken == []


@pytest.mark.parametrize(
    "outcome,result",
    [
        ("completed", _UNSET),
        ("not_started", "unexpected"),
        ("unknown", "unexpected"),
    ],
)
def test_result_shape_matches_outcome(outcome: str, result) -> None:
    service, operations = _service(_call("tool-1"))

    with pytest.raises(ValueError):
        _reconcile(service, outcome=outcome, result=result)

    assert operations.commit_count == 0
