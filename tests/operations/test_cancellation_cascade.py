"""Parent Operation 后代级联取消的双 Store 合同。"""

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import asyncio
from types import SimpleNamespace

import pytest

from pickel.conversations.agent_message import UserMessage
from pickel.conversations.content_blocks import TextBlock
from pickel.conversations.conversation_node import ConversationNode
from pickel.conversations.conversation_session import ConversationSession
from pickel.inbox.message import AgentMessageSource, UserMessageSource
from pickel.agents.agent_package import (
    AgentRuntimePolicy,
    ImplementationRef,
    ModelPolicy,
    ModelVersion,
    WorkspacePolicy,
    build_agent_package_version,
)
from pickel.operations.agent_run_state import (
    AgentRunState,
    Cancellation,
    DelegateAgentIntent,
    ModelStepState,
    ToolCallState,
)
from pickel.operations.agent_delegation import AgentDelegation
from pickel.operations.delegation_service import DelegationService
from pickel.operations.session_operation import SessionOperation
from pickel.operations.operation_service import OperationService
from pickel.persistence.in_memory_runtime_store import InMemoryRuntimeStore
from pickel.persistence.sqlite_runtime_store import SQLiteRuntimeStore
from pickel.runtime.operation_driver import OperationDriver
from pickel.runtime.runtime_effects import RuntimeEffects
from pickel.workspaces.workspace import Workspace
from pickel.workspaces.workspace_binding import WorkspaceBinding

NOW = datetime(2026, 8, 25, tzinfo=timezone.utc)


@pytest.fixture(params=("memory", "sqlite"))
def store(request: pytest.FixtureRequest, tmp_path: Path) -> Any:
    if request.param == "memory":
        return InMemoryRuntimeStore()
    return SQLiteRuntimeStore(tmp_path / "runtime.db")


def _package(agent_id: str):
    return build_agent_package_version(
        agent_id=agent_id,
        format_version=1,
        behavior_instruction=agent_id,
        model_policy=ModelPolicy(
            primary=ModelVersion(
                provider="test",
                model="test",
                wire_protocol="test",
                api_base=None,
                temperature=None,
                max_input_tokens=None,
                max_output_tokens=32,
                provider_options={},
                provider_implementation=ImplementationRef("provider", "test"),
                required_secret_refs=(),
            )
        ),
        runtime_policy=AgentRuntimePolicy(3, 8, 3),
        workspace_policy=WorkspacePolicy("workspace"),
        skills=(),
        tools=(),
        extensions=(),
        created_at=NOW,
    )


def _setup(store: Any, root: Path) -> tuple[Any, Any]:
    root.mkdir(parents=True)
    store.create_session(
        workspace=Workspace("workspace-1", root, NOW),
        session=ConversationSession(
            "session-1",
            "parent-agent",
            "workspace-1",
            root,
            None,
            None,
            None,
            None,
            NOW,
            NOW,
            None,
        ),
    )
    parent_package = _package("parent-agent")
    child_package = _package("child-agent")
    store.insert_agent_package_version(parent_package)
    store.insert_agent_package_version(child_package)
    message = store.send_message(
        message_id="message-1",
        session_id="session-1",
        delivery="followup",
        message=UserMessage((TextBlock("input"),)),
        source=UserMessageSource(),
        created_at=NOW,
    )
    operation = SessionOperation(
        "operation-1",
        "session-1",
        parent_package.package_version_id,
        WorkspaceBinding("workspace-1", root, root),
        message.message_id,
        NOW,
    )
    state = AgentRunState("operation-1", 1, "queued", None, 0, None, None, None, None)
    assert store.accept_operation(
        operation=operation, state=state, expected_node_id=None
    )
    call = ToolCallState(
        "tool-1",
        "delegate_agent",
        {},
        "intent_recorded",
        None,
        "safe",
        DelegateAgentIntent(child_package.package_version_id),
        None,
        None,
        None,
    )
    running = replace(
        state,
        revision=2,
        status="running",
        current_step=ModelStepState(
            "step-1", 1, "awaiting_tools", 0, None, "message-1", (call,)
        ),
    )
    assert store.commit_run_transition(
        state=running, expected_revision=1, node=None, updated_at=NOW
    )
    return parent_package, child_package


def _start_delegation(store: Any):
    return DelegationService(
        store=store,
        child_session_id_factory=lambda: "child-session-1",
        message_id_factory=lambda: "initial-message-1",
        now=lambda: NOW,
    ).start_delegation(
        "operation-1", "step-1", "tool-1", UserMessage((TextBlock("child"),))
    )


def _accept_child_operation(
    store: Any, child_session_id: str, package_id: str, root: Path
) -> None:
    operation = SessionOperation(
        "child-operation",
        child_session_id,
        package_id,
        WorkspaceBinding("workspace-1", root, root),
        "initial-message-1",
        NOW,
    )
    state = AgentRunState(
        "child-operation", 1, "queued", None, 0, None, None, None, None
    )
    assert store.accept_operation(
        operation=operation, state=state, expected_node_id=None
    )


def test_parent_cancel_reconciles_child_and_keeps_unrelated_messages(
    store: Any, tmp_path: Path
) -> None:
    _, child_package = _setup(store, tmp_path / "workspace")
    delegation = _start_delegation(store)
    _accept_child_operation(
        store,
        delegation.child_session_id,
        child_package.package_version_id,
        tmp_path / "workspace",
    )
    store.send_message(
        message_id="parent-followup",
        session_id=delegation.child_session_id,
        delivery="followup",
        message=UserMessage((TextBlock("parent followup"),)),
        source=AgentMessageSource("session-1", "operation-1", "followup"),
        created_at=NOW,
    )
    store.send_message(
        message_id="user-message",
        session_id=delegation.child_session_id,
        delivery="followup",
        message=UserMessage((TextBlock("user"),)),
        source=UserMessageSource(),
        created_at=NOW,
    )
    store.send_message(
        message_id="child-report",
        session_id="session-1",
        delivery="steer",
        message=UserMessage((TextBlock("report"),)),
        source=AgentMessageSource(
            delegation.child_session_id, "child-operation", "steer"
        ),
        created_at=NOW,
    )

    service = OperationService(store, now=lambda: NOW)
    assert service.request_cancellation("operation-1", reason="用户取消")
    assert store.load_run_state("child-operation").status == "cancelling"
    assert store.load_message(delegation.initial_message_id).status == "claimed"
    assert store.load_message("parent-followup").status == "discarded"
    assert store.load_message("user-message").status == "pending"
    assert store.load_message("child-report").status == "pending"
    assert not service.cancellation_ready("operation-1")

    child = service.load_agent_run_state("child-operation")
    child_cancelled = replace(
        child,
        revision=child.revision + 1,
        status="cancelled",
        cancellation=child.cancellation,
        current_step=None,
    )
    assert service.commit_transition(
        state=child_cancelled,
        expected_revision=child.revision,
        node=None,
        updated_at=NOW,
    )
    assert service.cancellation_ready("operation-1")

    parent = service.load_agent_run_state("operation-1")
    parent_cancelled = replace(
        parent,
        revision=parent.revision + 1,
        status="cancelled",
        current_step=None,
    )
    assert service.commit_transition(
        state=parent_cancelled,
        expected_revision=parent.revision,
        node=None,
        updated_at=NOW,
    )


def test_parent_terminal_cas_is_blocked_until_child_is_terminal(
    store: Any, tmp_path: Path
) -> None:
    _, child_package = _setup(store, tmp_path / "workspace")
    delegation = _start_delegation(store)
    _accept_child_operation(
        store,
        delegation.child_session_id,
        child_package.package_version_id,
        tmp_path / "workspace",
    )
    service = OperationService(store, now=lambda: NOW)
    assert service.request_cancellation("operation-1", reason="用户取消")
    parent = service.load_agent_run_state("operation-1")
    assert not service.commit_transition(
        state=replace(parent, revision=parent.revision + 1, status="cancelled"),
        expected_revision=parent.revision,
        node=None,
        updated_at=NOW,
    )
    assert service.load_agent_run_state("operation-1").status == "cancelling"


def test_parent_cancel_recurses_to_grandchild_and_discards_child_message(
    store: Any, tmp_path: Path
) -> None:
    _, child_package = _setup(store, tmp_path / "workspace")
    child_delegation = _start_delegation(store)
    _accept_child_operation(
        store,
        child_delegation.child_session_id,
        child_package.package_version_id,
        tmp_path / "workspace",
    )
    store.create_session(
        workspace=Workspace("workspace-1", tmp_path / "workspace", NOW),
        session=ConversationSession(
            "grandchild-session",
            "child-agent",
            "workspace-1",
            tmp_path / "workspace",
            None,
            None,
            None,
            None,
            NOW,
            NOW,
            None,
        ),
    )
    initial = store.send_message(
        message_id="grandchild-initial",
        session_id="grandchild-session",
        delivery="followup",
        message=UserMessage((TextBlock("grandchild"),)),
        source=AgentMessageSource(
            child_delegation.child_session_id, "child-operation", "followup"
        ),
        created_at=NOW,
    )
    store.insert_delegation(
        AgentDelegation(
            "grandchild-session",
            "child-operation",
            "child-step",
            "child-tool",
            initial.message_id,
            NOW,
        )
    )
    grandchild_operation = SessionOperation(
        "grandchild-operation",
        "grandchild-session",
        child_package.package_version_id,
        WorkspaceBinding("workspace-1", tmp_path / "workspace", tmp_path / "workspace"),
        initial.message_id,
        NOW,
    )
    assert store.accept_operation(
        operation=grandchild_operation,
        state=AgentRunState(
            "grandchild-operation", 1, "queued", None, 0, None, None, None, None
        ),
        expected_node_id=None,
    )
    store.send_message(
        message_id="child-followup",
        session_id="grandchild-session",
        delivery="followup",
        message=UserMessage((TextBlock("continue"),)),
        source=AgentMessageSource(
            child_delegation.child_session_id, "child-operation", "followup"
        ),
        created_at=NOW,
    )

    service = OperationService(store, now=lambda: NOW)
    assert service.request_cancellation("operation-1", reason="用户取消")

    assert service.load_agent_run_state("child-operation").status == "cancelling"
    assert service.load_agent_run_state("grandchild-operation").status == "cancelling"
    assert store.load_message("child-followup").status == "discarded"


def test_service_has_no_wake_dependency_and_driver_wakes_cascade_targets() -> None:
    operation = SessionOperation(
        "child-operation",
        "child-session",
        "agentpkg_" + "a" * 64,
        WorkspaceBinding("workspace-1", Path.cwd(), Path.cwd()),
        "node-1",
        NOW,
    )
    state = AgentRunState(
        "child-operation",
        2,
        "cancelling",
        None,
        0,
        None,
        None,
        None,
        Cancellation("parent cancelled", NOW),
    )

    class Operations:
        def __init__(self) -> None:
            self.state = state

        def load_operation(self, operation_id: str):
            assert operation_id == operation.operation_id
            return operation

        def load_agent_run_state(self, operation_id: str):
            return self.state

        def reconcile_cancellation(self, operation_id: str, *, reason: str | None):
            return ("sibling-session",)

        def cancellation_ready(self, operation_id: str) -> bool:
            return True

        def commit_transition(self, *, state, expected_revision, node, updated_at):
            self.state = state
            return True

        def parent_session_id(self, operation_id: str) -> str | None:
            return "parent-session"

    operations = Operations()
    wake_sessions: list[str] = []
    input_node = ConversationNode(
        node_id="node-1",
        session_id="child-session",
        parent_node_id=None,
        content_type="agent_message",
        content=UserMessage(),
        created_at=NOW,
    )
    conversations = SimpleNamespace(
        load_conversation_session=lambda session_id: SimpleNamespace(
            active_node_id=input_node.node_id
        ),
        list_branch_nodes=lambda **kwargs: [input_node],
    )
    driver = OperationDriver(
        operation_service=operations,
        conversation_service=conversations,
        package_loader=lambda candidate: SimpleNamespace(
            package_version_id=candidate.agent_package_version_id,
            runtime_policy=SimpleNamespace(max_model_steps=3),
        ),
        effects_resolver=lambda candidate: RuntimeEffects(provider=object()),
        wake_callback=wake_sessions.append,
    )
    result = asyncio.run(driver.drive_operation("child-operation"))

    assert result.status == "cancelled"
    assert result.usage is not None
    assert result.usage.steps == 0
    assert wake_sessions == ["sibling-session", "parent-session"]
