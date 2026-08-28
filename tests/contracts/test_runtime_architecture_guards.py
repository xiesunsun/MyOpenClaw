"""v10 Runtime 架构护栏合同。

测试只经过 ConversationService、Inbox、OperationService、OperationDriver 和
RuntimeEffects；不依赖旧的运行时资源袋。
"""

from __future__ import annotations

import asyncio
import importlib
import ast
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
from pickel.context.model_context import ModelContext
from pickel.conversations.agent_message import AssistantMessage, UserMessage
from pickel.conversations.content_blocks import TextBlock
from pickel.conversations.conversation_service import ConversationService
from pickel.inbox.message import UserMessageSource
from pickel.operations.operation_service import OperationService
from pickel.persistence.sqlite_runtime_store import SQLiteRuntimeStore
from pickel.providers.base import Provider
from pickel.providers.prepared import PreparedModelCall
from pickel.providers.stream import StreamCompleted
from pickel.runtime.operation_driver import OperationDriver
from pickel.runtime.runtime_effects import RuntimeEffects
from pickel.workspaces.workspace_binding import WorkspaceBinding


def test_unwired_agent_run_progress_is_not_a_public_runtime_api() -> None:
    """未接线的 AgentRun 进度通知不能继续作为 Runtime 公共模块存在。"""
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("pickel.runtime.agent_run_progress")


@pytest.mark.parametrize(
    "retired_module",
    [
        "pickel.runtime.agent_run_state_machine",
        "pickel.context.templates_loader",
        "pickel.model_calls.prepared",
        "pickel.observe.records",
        "pickel.tools.wait_delegation",
    ],
)
def test_retired_cross_layer_modules_do_not_return(retired_module: str) -> None:
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(retired_module)


def test_operations_do_not_import_persistence_adapters() -> None:
    root = Path(__file__).parents[2] / "src" / "pickel" / "operations"
    for path in root.glob("*.py"):
        tree = ast.parse(path.read_text())
        imported = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        assert not any(name.startswith("pickel.persistence") for name in imported), path


def test_supported_provider_modules_have_no_legacy_generation_entrypoints() -> None:
    root = Path(__file__).parents[2] / "src" / "pickel" / "providers"
    for path in root.glob("*.py"):
        if path.name in {"gemini.py", "__init__.py"}:
            continue
        tree = ast.parse(path.read_text())
        names = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        assert not names.intersection({"generate", "stream"}), path


class _Provider(Provider):
    artifact_service = None
    request_cache_order = ("tools", "system", "messages")

    def __init__(self, *, snapshot: bool = False) -> None:
        self.snapshot_enabled = snapshot
        self.contexts: list[ModelContext] = []

    @classmethod
    def from_config(cls, config):
        return cls()

    def prepare(self, context: ModelContext) -> PreparedModelCall:
        self.contexts.append(context)
        return PreparedModelCall(
            provider="anthropic",
            api_kind="anthropic-messages",
            endpoint="messages",
            requested_model="claude-test",
            body={"stream": True},
        )

    async def stream_prepared(self, prepared: PreparedModelCall):
        assert prepared.api_kind == "anthropic-messages"
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


def test_trace_is_not_model_request_authority() -> None:
    """可靠 actual request 只能来自 ModelCall RequestContent。"""
    assert not hasattr(RuntimeEffects, "execute_model_request")
    assert not hasattr(Provider, "request_snapshot")
