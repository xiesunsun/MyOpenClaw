"""v10 Runtime 架构护栏合同。

测试只经过 ConversationService、Inbox、OperationService、OperationDriver 和
RuntimeEffects；不依赖旧的运行时资源袋。
"""

from __future__ import annotations

import asyncio
import importlib
from datetime import datetime, timezone
from pathlib import Path

import pytest

from pickel.agents.agent_package import (
    AgentPackageVersion,
    AgentRuntimePolicy,
    ImplementationRef,
    ModelPolicy,
    ModelVersion,
    WorkspacePolicy,
    build_agent_package_version,
)
from pickel.context.model_context import ModelContext, SystemContent
from pickel.conversations.agent_message import AssistantMessage, UserMessage
from pickel.conversations.content_blocks import TextBlock
from pickel.conversations.conversation_service import ConversationService
from pickel.inbox.message import UserMessageSource
from pickel.operations.agent_run_state import (
    AgentRunState,
    ModelRequestIntent,
    ModelStepState,
)
from pickel.operations.operation_service import OperationService
from pickel.operations.session_operation import SessionOperation
from pickel.persistence.sqlite_runtime_store import SQLiteRuntimeStore
from pickel.observe.records import RequestSnapshotRecord, observation_scope
from pickel.providers.base import Provider
from pickel.providers.stream import StreamCompleted
from pickel.runtime.operation_driver import OperationDriver
from pickel.runtime.runtime_effects import RuntimeEffects
from pickel.workspaces.workspace_binding import WorkspaceBinding


def test_unwired_agent_run_progress_is_not_a_public_runtime_api() -> None:
    """未接线的 AgentRun 进度通知不能继续作为 Runtime 公共模块存在。"""
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("pickel.runtime.agent_run_progress")


class _Provider(Provider):
    artifact_service = None
    request_cache_order = ("tools", "system", "messages")

    def __init__(self, *, snapshot: bool = False) -> None:
        self.snapshot_enabled = snapshot
        self.contexts: list[ModelContext] = []

    @classmethod
    def from_config(cls, config):
        return cls()

    async def generate(self, context: ModelContext) -> AssistantMessage:
        raise AssertionError("合同测试要求 Runtime 使用 stream")

    async def stream(self, context: ModelContext):
        self.contexts.append(context)
        yield StreamCompleted(message=AssistantMessage(content=(TextBlock("ok"),)))

    def request_snapshot(self, context: ModelContext) -> dict | None:
        if not self.snapshot_enabled:
            return None
        return {
            "system": context.system.as_text(),
            "message_count": len(context.messages),
        }


class _Observer:
    def __init__(self) -> None:
        self.records = []

    def wants(self, capability: str) -> bool:
        return capability == "request_snapshot"

    def record(self, record) -> None:
        self.records.append(record)


def _package(*, behavior: str = "Be helpful.") -> AgentPackageVersion:
    return build_agent_package_version(
        agent_id="Pickle",
        format_version=1,
        behavior_instruction=behavior,
        model_policy=ModelPolicy(
            primary=ModelVersion(
                provider="anthropic",
                model="claude-test",
                wire_protocol="anthropic-messages",
                api_base=None,
                temperature=None,
                max_input_tokens=None,
                max_output_tokens=1024,
                provider_options={},
                provider_implementation=ImplementationRef(
                    "provider", "anthropic-messages"
                ),
                required_secret_refs=(),
            )
        ),
        runtime_policy=AgentRuntimePolicy(max_model_steps=8, context_turn_window=5),
        workspace_policy=WorkspacePolicy("workspace"),
        skills=(),
        tools=(),
        extensions=(),
        created_at=datetime(2026, 8, 25, tzinfo=timezone.utc),
    )


def test_resume_rejects_runtime_package_different_from_accepted_operation(
    tmp_path,
) -> None:
    """恢复入口不能把已接受 Operation 静默切换到另一 Package。"""
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    conversation = ConversationService(store, session_id_factory=lambda: "session-1")
    session = conversation.create_conversation_session(
        agent_id="Pickle", cwd=str(tmp_path)
    )
    accepted_package = _package(behavior="accepted package")
    replacement_package = _package(behavior="replacement package")
    store.insert_agent_package_version(accepted_package)

    inbox = store.send_message(
        message_id="message-1",
        session_id=session.session_id,
        delivery="followup",
        message=UserMessage(content=(TextBlock("resume me"),)),
        source=UserMessageSource(),
        created_at=datetime(2026, 8, 25, tzinfo=timezone.utc),
    )
    operation_service = OperationService(
        store,
        operation_id_factory=lambda: "operation-1",
        now=lambda: datetime(2026, 8, 25, tzinfo=timezone.utc),
    )
    accepted = operation_service.accept_pending_message(
        message=inbox,
        agent_package_version_id=accepted_package.package_version_id,
        workspace_binding=WorkspaceBinding(
            workspace_id=session.workspace_id,
            working_directory=session.cwd,
            allowed_root=session.cwd,
        ),
        expected_node_id=None,
    )
    assert accepted is not None

    driver = OperationDriver(
        operation_service=operation_service,
        conversation_service=conversation,
        package_loader=lambda package_id: replacement_package,
        effects_resolver=lambda package_id: RuntimeEffects(provider=_Provider()),
    )

    with pytest.raises(RuntimeError, match="错误的 AgentPackageVersion"):
        asyncio.run(driver.drive_operation(accepted.operation.operation_id))


def test_model_request_exposes_actual_context_and_full_request_snapshot() -> None:
    """Provider 与 Observer 必须看到同一个已冻结 ModelContext。"""
    provider = _Provider(snapshot=True)
    context = ModelContext(
        system=SystemContent.from_text("system contract"),
        messages=(UserMessage(content=(TextBlock("actual input"),)),),
    )
    state = AgentRunState(
        operation_id="operation-1",
        revision=2,
        status="running",
        waiting_reason=None,
        completed_step_count=0,
        current_step=ModelStepState(
            step_id="step-1",
            step_sequence=1,
            phase="request_ready",
            request_attempt=0,
            request_intent=ModelRequestIntent(context, "context-fingerprint"),
            assistant_message_node_id=None,
            tool_calls=(),
        ),
        final_assistant_node_id=None,
        error=None,
        cancellation=None,
    )
    operation = SessionOperation(
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
    effects = RuntimeEffects(
        provider=provider,
        provider_name="anthropic",
        model_name="claude-test",
    )
    observer = _Observer()

    with observation_scope(observer):
        asyncio.run(
            effects.execute_model_request(
                operation=operation,
                state=state,
                model_context=context,
            )
        )

    assert provider.contexts == [context]
    snapshots = [
        record
        for record in observer.records
        if isinstance(record, RequestSnapshotRecord)
    ]
    assert len(snapshots) == 1
    snapshot = snapshots[0]
    assert snapshot.provider == "anthropic"
    assert snapshot.model == "claude-test"
    assert snapshot.request == {"system": "system contract", "message_count": 1}
    assert snapshot.cache_order == ("tools", "system", "messages")
    assert snapshot.identity.session_id == "session-1"
    assert snapshot.identity.operation_id == "operation-1"
    assert snapshot.identity.step_id == "step-1"
    assert snapshot.identity.step_sequence == 1
