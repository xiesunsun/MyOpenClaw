from __future__ import annotations

import os
import asyncio
import textwrap
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from pickel.agents.agent_package_loader import PackageLoadError
from pickel.agents.agent_package import package_version_id_for_content
from pickel.app.boot import Boot
from pickel.artifacts.artifact_service import ArtifactIntegrityError
from pickel.app.runtime_host import RuntimeHost, _RuntimeDelegationControl
from pickel.app.runtime_models import ConversationRequest
from pickel.conversations.agent_message import UserMessage
from pickel.conversations.content_blocks import TextBlock
from pickel.extensions_host.loader import LoadResult
from pickel.inbox.message import AgentMessageSource, InboxMessage, UserMessageSource
from pickel.operations.operation_service import OperationService
from pickel.operations.agent_delegation import AgentDelegation
from pickel.operations.agent_run_state import DelegateAgentIntent
from pickel.runtime.agent_registry import AgentRegistry
from pickel.workspaces.workspace_binding import WorkspaceBinding
from tests.helpers.yaml_app_config import app_config_from_yaml_file


def _boot(root: Path) -> Boot:
    (root / "agents" / "Pickle").mkdir(parents=True)
    (root / "agents" / "Pickle" / "AGENT.md").write_text(
        "You are Pickle.\n",
        encoding="utf-8",
    )
    (root / "workspace").mkdir()
    config_path = root / "config.yaml"
    config_path.write_text(
        textwrap.dedent("""
            default_agent: Pickle
            default_llm:
              provider: anthropic
              model: claude-test
            providers:
              anthropic:
                models:
                  claude-test:
                    max_output_tokens: 1024
                    provider_options: {}
            agents:
              Pickle:
                workspace_path: workspace
                behavior_path: agents/Pickle
            """).strip(),
        encoding="utf-8",
    )
    return Boot.from_config(app_config_from_yaml_file(config_path))


def _headless_fixture(host: RuntimeHost, root: Path, *, active_operation: bool = False):
    conversation = host.open_conversation(
        ConversationRequest(agent_id="Pickle", cwd=root)
    )
    parent_loaded = host.active_generation.loaded_packages[
        conversation.agent_definition.package_version_id
    ]
    child_content = parent_loaded.version.content_dict()
    child_content["behavior_instruction"] = "child package"
    child_package_id = package_version_id_for_content(child_content)
    child_loaded = replace(
        parent_loaded,
        version=replace(
            parent_loaded.version,
            package_version_id=child_package_id,
            behavior_instruction="child package",
        ),
    )
    intent_package_id = "agentpkg_" + "d" * 64 if active_operation else child_package_id
    child_session = replace(
        conversation.session,
        session_id="child-session-1",
        active_operation_id="child-operation-1" if active_operation else None,
    )
    parent_operation = SimpleNamespace(
        operation_id="parent-operation-1",
        session_id=conversation.session.session_id,
        agent_package_version_id=conversation.agent_definition.package_version_id,
    )
    parent_state = SimpleNamespace(
        current_step=SimpleNamespace(
            step_id="parent-step-1",
            tool_calls=(
                SimpleNamespace(
                    tool_call_id="parent-tool-1",
                    execution_intent=DelegateAgentIntent(intent_package_id),
                ),
            ),
        )
    )
    child_operation = SimpleNamespace(
        operation_id="child-operation-1",
        session_id=child_session.session_id,
        agent_package_version_id=child_package_id,
    )

    class Store:
        def __init__(self):
            self.sessions = {child_session.session_id: child_session}
            self.inserted = []

        def load_session(self, session_id):
            return self.sessions.get(session_id)

        def load_delegation(self, session_id):
            if active_operation:
                return None
            return SimpleNamespace(
                child_session_id=session_id,
                parent_operation_id=parent_operation.operation_id,
                parent_step_id="parent-step-1",
                parent_tool_call_id="parent-tool-1",
            )

        def load_operation(self, operation_id):
            if operation_id == parent_operation.operation_id:
                return parent_operation
            if operation_id == child_operation.operation_id:
                return child_operation
            return None

        def load_run_state(self, operation_id):
            if operation_id == parent_operation.operation_id:
                return parent_state
            return None

        def insert_agent_package_version(self, package):
            self.inserted.append(package.package_version_id)

    return conversation, Store(), child_loaded, child_package_id


def test_runtime_host_creates_and_resumes_persistent_conversation() -> None:
    with TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        with patch.dict(os.environ, {"PICKEL_HOME": str(root / "home")}):
            host = RuntimeHost(_boot(root))
            first = host.open_conversation(
                ConversationRequest(agent_id="Pickle", cwd=root)
            )
            session_id = first.session.session_id
            resumed = host.open_conversation(ConversationRequest(session_id=session_id))
            live_agent = host.agent_registry.get(session_id)

        assert resumed.session.session_id == session_id
        assert resumed is first
        assert resumed.agent_definition.agent_id == "Pickle"
        assert live_agent is not None
        assert host.agent_registry.get(session_id) is live_agent
        assert live_agent._driver._wake_callback == host.agent_registry.wake
        assert (root / "home" / "runtime.db").exists()


def test_async_create_discovers_runnable_sessions_from_shared_store() -> None:
    with TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        boot = _boot(root)
        store = SimpleNamespace(
            list_runnable_session_ids=lambda: ("session-a", "session-b")
        )
        seen: list[tuple[str, object]] = []

        async def activate(self, session_id, candidate_store):
            seen.append((session_id, candidate_store))

        with (
            patch(
                "pickel.app.runtime_host.load_extensions_async",
                new=AsyncMock(return_value=LoadResult()),
            ),
            patch.object(boot, "runtime_store", return_value=store),
            patch.object(RuntimeHost, "activate_agent", new=activate),
        ):
            host = asyncio.run(
                RuntimeHost.create(
                    boot.app_config,
                    boot_factory=lambda *_args, **_kwargs: boot,
                )
            )

        assert host.boot is boot
        assert [item[0] for item in seen] == ["session-a", "session-b"]
        assert all(item[1] is store for item in seen)


def test_startup_recovery_isolates_candidate_failure(caplog) -> None:
    with TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        host = RuntimeHost(_boot(root))
        store = SimpleNamespace(list_runnable_session_ids=lambda: ("bad", "good"))
        calls: list[str] = []

        async def activate(session_id, _store):
            calls.append(session_id)
            if session_id == "bad":
                raise RuntimeError("broken package")

        with patch.object(host, "activate_agent", new=activate):
            with patch.object(host.boot, "runtime_store", return_value=store):
                asyncio.run(host._recover_runnable_sessions())

        assert calls == ["bad", "good"]
        assert "启动恢复 Session 失败" in caplog.text


def test_runtime_delegation_control_activates_accepted_child() -> None:
    store = object()
    registry = SimpleNamespace(wake=Mock())
    host = SimpleNamespace(activate_agent=AsyncMock(), agent_registry=registry)
    delegation = AgentDelegation(
        child_session_id="child-session",
        parent_operation_id="parent-operation",
        parent_step_id="parent-step",
        parent_tool_call_id="parent-tool",
        initial_message_id="child-message",
        created_at=datetime(2026, 8, 25, tzinfo=timezone.utc),
    )
    service = SimpleNamespace(start_delegation=lambda *_args: delegation)

    with patch(
        "pickel.app.runtime_host.DelegationService", return_value=service
    ) as service_type:
        result = asyncio.run(
            _RuntimeDelegationControl(host, store).start_delegation(
                parent_operation_id="parent-operation",
                parent_step_id="parent-step",
                parent_tool_call_id="parent-tool",
                message=UserMessage(),
            )
        )

    assert result == delegation
    service_type.assert_called_once_with(store=store)
    host.activate_agent.assert_awaited_once_with("child-session", store)
    registry.wake.assert_called_once_with("child-session")


def test_runtime_delegation_control_preserves_acceptance_on_activation_failure(
    caplog,
) -> None:
    store = object()
    host = SimpleNamespace(
        activate_agent=AsyncMock(side_effect=RuntimeError("package unavailable"))
    )
    delegation = AgentDelegation(
        child_session_id="child-session",
        parent_operation_id="parent-operation",
        parent_step_id="parent-step",
        parent_tool_call_id="parent-tool",
        initial_message_id="child-message",
        created_at=datetime(2026, 8, 25, tzinfo=timezone.utc),
    )
    service = SimpleNamespace(start_delegation=lambda *_args: delegation)

    with patch("pickel.app.runtime_host.DelegationService", return_value=service):
        result = asyncio.run(
            _RuntimeDelegationControl(host, store).start_delegation(
                parent_operation_id="parent-operation",
                parent_step_id="parent-step",
                parent_tool_call_id="parent-tool",
                message=UserMessage(),
            )
        )

    assert result == delegation
    assert "Child Session 激活失败" in caplog.text


def test_runtime_delegation_control_lists_children_without_activation() -> None:
    store = object()
    registry = SimpleNamespace(wake=Mock())
    host = SimpleNamespace(activate_agent=AsyncMock(), agent_registry=registry)
    service = SimpleNamespace(list_child_agents=lambda *_args: ())

    with patch(
        "pickel.app.runtime_host.DelegationService", return_value=service
    ) as service_type:
        result = asyncio.run(
            _RuntimeDelegationControl(host, store).list_child_agents(
                sender_operation_id="parent-operation",
                sender_step_id="parent-step",
                sender_tool_call_id="list-tool",
            )
        )

    assert result == ()
    service_type.assert_called_once_with(store=store)
    host.activate_agent.assert_not_awaited()
    registry.wake.assert_not_called()


def test_runtime_delegation_control_reports_to_parent_and_wakes() -> None:
    store = object()
    registry = SimpleNamespace(wake=Mock())
    host = SimpleNamespace(activate_agent=AsyncMock(), agent_registry=registry)
    stored = InboxMessage(
        message_id="message-report",
        session_id="parent-session",
        sequence=2,
        delivery="steer",
        message=UserMessage((TextBlock("report"),)),
        source=AgentMessageSource(
            sender_session_id="child-session",
            sender_operation_id="child-operation",
            form="steer",
        ),
        created_at=datetime(2026, 8, 25, tzinfo=timezone.utc),
    )
    service = SimpleNamespace(send_child_report=lambda *_args: stored)

    with patch("pickel.app.runtime_host.DelegationService", return_value=service):
        result = asyncio.run(
            _RuntimeDelegationControl(host, store).send_child_report(
                sender_operation_id="child-operation",
                sender_step_id="report-step",
                sender_tool_call_id="report-tool",
                output="report",
            )
        )

    assert result == stored
    host.activate_agent.assert_awaited_once_with("parent-session", store)
    registry.wake.assert_called_once_with("parent-session")


def test_runtime_delegation_control_keeps_report_when_parent_activation_fails(
    caplog,
) -> None:
    store = object()
    host = SimpleNamespace(
        activate_agent=AsyncMock(side_effect=RuntimeError("package unavailable")),
        agent_registry=SimpleNamespace(wake=Mock()),
    )
    stored = InboxMessage(
        message_id="message-report",
        session_id="parent-session",
        sequence=2,
        delivery="steer",
        message=UserMessage((TextBlock("report"),)),
        source=AgentMessageSource(
            sender_session_id="child-session",
            sender_operation_id="child-operation",
            form="steer",
        ),
        created_at=datetime(2026, 8, 25, tzinfo=timezone.utc),
    )
    service = SimpleNamespace(send_child_report=lambda *_args: stored)

    with patch("pickel.app.runtime_host.DelegationService", return_value=service):
        result = asyncio.run(
            _RuntimeDelegationControl(host, store).send_child_report(
                sender_operation_id="child-operation",
                sender_step_id="report-step",
                sender_tool_call_id="report-tool",
                output="report",
            )
        )

    assert result == stored
    assert "Parent Session 激活失败" in caplog.text
    host.agent_registry.wake.assert_not_called()


def test_headless_activation_is_idempotent_and_shutdown_releases_handle() -> None:
    with TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        with patch.dict(os.environ, {"PICKEL_HOME": str(root / "home")}):
            host = RuntimeHost(_boot(root))
            conversation = host.open_conversation(
                ConversationRequest(agent_id="Pickle", cwd=root)
            )
            # UI Conversation 已占用 Registry 时，activation 仍保持同一 Agent 身份。
            first = asyncio.run(
                host.activate_agent(
                    conversation.session.session_id, conversation.persistence_store
                )
            )
            generation = host.active_generation
            refs = generation.operation_ref_count
            second = asyncio.run(
                host.activate_agent(
                    conversation.session.session_id, conversation.persistence_store
                )
            )
            assert first is second
            assert refs == generation.operation_ref_count
            asyncio.run(host.shutdown())
            assert generation.operation_ref_count == 0
            assert generation.closed


def test_headless_delegation_uses_intent_package_registers_before_wake() -> None:
    with TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        with patch.dict(os.environ, {"PICKEL_HOME": str(root / "home")}):
            host = RuntimeHost(_boot(root))
            conversation, store, child_loaded, child_package_id = _headless_fixture(
                host, root
            )
            events: list[str] = []
            built = SimpleNamespace(session_id="child-session-1")
            with (
                patch.object(
                    host.boot,
                    "load_agent_package",
                    return_value=child_loaded,
                ) as load_package,
                patch.object(
                    host.boot,
                    "resolve_loaded_agent_package",
                    side_effect=AssertionError("delegated child 不能 resolve 当前配置"),
                ),
                patch.object(host.boot, "build_agent", return_value=built),
                patch.object(
                    host.agent_registry,
                    "register",
                    side_effect=lambda agent: (
                        events.append("register"),
                        AgentRegistry.register(host.agent_registry, agent),
                    )[1],
                ),
                patch.object(
                    host.agent_registry,
                    "wake",
                    side_effect=lambda session_id: events.append(f"wake:{session_id}"),
                ),
            ):
                baseline = host.active_generation.operation_ref_count
                first = asyncio.run(host.activate_agent("child-session-1", store))
                refs = host.active_generation.operation_ref_count
                second = asyncio.run(host.activate_agent("child-session-1", store))

            assert first is built is second
            load_package.assert_called_once_with(
                child_package_id,
                store=store,
                artifact_service=host._artifact_service_for(store),
                expected_agent_id="Pickle",
            )
            assert events == ["register", "wake:child-session-1"]
            assert refs == baseline + 1
            assert host.active_generation.operation_ref_count == refs
            conversation.detach()
            asyncio.run(host.shutdown())


def test_headless_active_operation_package_wins_over_parent_intent() -> None:
    with TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        with patch.dict(os.environ, {"PICKEL_HOME": str(root / "home")}):
            host = RuntimeHost(_boot(root))
            conversation, store, child_loaded, child_package_id = _headless_fixture(
                host, root, active_operation=True
            )
            built = SimpleNamespace(session_id="child-session-1")
            with (
                patch.object(
                    host.boot, "load_agent_package", return_value=child_loaded
                ) as load_package,
                patch.object(host.boot, "build_agent", return_value=built),
                patch.object(host.agent_registry, "wake"),
            ):
                asyncio.run(host.activate_agent("child-session-1", store))
            load_package.assert_called_once_with(
                child_package_id,
                store=store,
                artifact_service=host._artifact_service_for(store),
                expected_agent_id="Pickle",
            )
            conversation.detach()
            asyncio.run(host.shutdown())


def test_active_operation_missing_package_uses_current_shell_only() -> None:
    with TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        with patch.dict(os.environ, {"PICKEL_HOME": str(root / "home")}):
            host = RuntimeHost(_boot(root))
            conversation, store, current_loaded, child_package_id = _headless_fixture(
                host, root, active_operation=True
            )
            missing = PackageLoadError("missing", child_package_id, "not found")
            built = SimpleNamespace(session_id="child-session-1")
            with (
                patch.object(host.boot, "load_agent_package", side_effect=missing),
                patch.object(
                    host.boot,
                    "resolve_loaded_agent_package",
                    return_value=current_loaded,
                ) as resolve,
                patch.object(host.boot, "build_agent", return_value=built),
                patch.object(host.agent_registry, "wake"),
            ):
                result = asyncio.run(host.activate_agent("child-session-1", store))

            assert result is built
            resolve.assert_called_once_with(
                "Pickle", artifact_service=host._artifact_service_for(store)
            )
            conversation.detach()
            asyncio.run(host.shutdown())


def test_headless_build_failure_releases_handle_without_touching_durable_child() -> (
    None
):
    with TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        with patch.dict(os.environ, {"PICKEL_HOME": str(root / "home")}):
            host = RuntimeHost(_boot(root))
            conversation, store, child_loaded, _child_package_id = _headless_fixture(
                host, root
            )
            baseline = host.active_generation.operation_ref_count
            with (
                patch.object(
                    host.boot, "load_agent_package", return_value=child_loaded
                ),
                patch.object(
                    host.boot,
                    "build_agent",
                    side_effect=RuntimeError("build failed"),
                ),
            ):
                with pytest.raises(RuntimeError, match="build failed"):
                    asyncio.run(host.activate_agent("child-session-1", store))
            assert host.active_generation.operation_ref_count == baseline
            assert host.agent_registry.get("child-session-1") is None
            assert "child-session-1" not in host._headless_agents
            assert store.load_session("child-session-1") is not None
            conversation.detach()
            asyncio.run(host.shutdown())


def test_headless_registry_failure_releases_handle_without_registration() -> None:
    with TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        with patch.dict(os.environ, {"PICKEL_HOME": str(root / "home")}):
            host = RuntimeHost(_boot(root))
            conversation, store, child_loaded, _child_package_id = _headless_fixture(
                host, root
            )
            baseline = host.active_generation.operation_ref_count
            with (
                patch.object(
                    host.boot, "load_agent_package", return_value=child_loaded
                ),
                patch.object(
                    host.boot,
                    "build_agent",
                    return_value=SimpleNamespace(session_id="child-session-1"),
                ),
                patch.object(
                    host.agent_registry,
                    "register",
                    side_effect=RuntimeError("register failed"),
                ),
            ):
                with pytest.raises(RuntimeError, match="register failed"):
                    asyncio.run(host.activate_agent("child-session-1", store))
            assert host.active_generation.operation_ref_count == baseline
            assert host.agent_registry.get("child-session-1") is None
            assert "child-session-1" not in host._headless_agents
            assert store.load_session("child-session-1") is not None
            conversation.detach()
            asyncio.run(host.shutdown())


def test_runtime_host_keeps_ephemeral_conversation_off_disk() -> None:
    with TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        with patch.dict(os.environ, {"PICKEL_HOME": str(root / "home")}):
            conversation = RuntimeHost(_boot(root)).open_conversation(
                ConversationRequest(
                    agent_id="Pickle",
                    persistence="ephemeral",
                    cwd=root,
                )
            )

        assert conversation.persistence == "ephemeral"
        assert not (root / "home" / "runtime.db").exists()


def test_runtime_host_reuses_artifact_service_for_same_store() -> None:
    with TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        with patch.dict(os.environ, {"PICKEL_HOME": str(root / "home")}):
            host = RuntimeHost(_boot(root))
            first = host.open_conversation(
                ConversationRequest(agent_id="Pickle", cwd=root)
            )
            store = first.persistence_store
            service = host._artifact_service_for(store)

            loaded = host.boot.load_agent_package(
                first.agent_definition.package_version_id,
                store=store,
                artifact_service=service,
                expected_agent_id="Pickle",
            )

            assert host._artifact_service_for(store) is service
            assert loaded.model_clients["primary"].artifact_service is service

            first.detach()
            asyncio.run(host.shutdown())


def test_runtime_host_isolates_artifact_services_for_in_memory_stores() -> None:
    with TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        with patch.dict(os.environ, {"PICKEL_HOME": str(root / "home")}):
            host = RuntimeHost(_boot(root))
            first = host.open_conversation(
                ConversationRequest(
                    agent_id="Pickle", persistence="ephemeral", cwd=root
                )
            )
            second = host.open_conversation(
                ConversationRequest(
                    agent_id="Pickle", persistence="ephemeral", cwd=root
                )
            )
            first_service = host._artifact_service_for(first.persistence_store)
            second_service = host._artifact_service_for(second.persistence_store)
            reference = first_service.create_artifact(
                data=b"store-one", media_type="text/plain"
            )

            assert first.persistence_store is not second.persistence_store
            assert first_service is not second_service
            with pytest.raises(ArtifactIntegrityError):
                second_service.load_artifact_bytes(reference)

            first.detach()
            second.detach()
            asyncio.run(host.shutdown())


def test_ephemeral_reload_keeps_artifact_bytes_readable() -> None:
    with TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        with patch.dict(os.environ, {"PICKEL_HOME": str(root / "home")}):
            host = RuntimeHost(_boot(root))
            conversation = host.open_conversation(
                ConversationRequest(
                    agent_id="Pickle", persistence="ephemeral", cwd=root
                )
            )
            store = conversation.persistence_store
            service = host._artifact_service_for(store)
            reference = service.create_artifact(
                data=b"survives-reload", media_type="text/plain"
            )

            result = asyncio.run(host.reload(conversation, app_config=host.app_config))

            assert host._artifact_service_for(store).load_artifact_bytes(reference) == (
                b"survives-reload"
            )
            result.conversation.detach()
            asyncio.run(host.shutdown())


def test_direct_conversation_detach_unregisters_agent_before_reopen() -> None:
    with TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        with patch.dict(os.environ, {"PICKEL_HOME": str(root / "home")}):
            host = RuntimeHost(_boot(root))
            conversation = host.open_conversation(
                ConversationRequest(agent_id="Pickle", cwd=root)
            )
            session_id = conversation.session.session_id
            old_agent = host.agent_registry.get(session_id)
            assert old_agent is not None

            conversation.detach()

            assert host.agent_registry.get(session_id) is None
            reopened = host.open_conversation(
                ConversationRequest(session_id=session_id)
            )
            assert host.agent_registry.get(session_id) is not old_agent
            assert reopened is not conversation


def test_runtime_host_keeps_recovery_path_open_when_exact_package_cannot_load() -> None:
    with TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        with patch.dict(os.environ, {"PICKEL_HOME": str(root / "home")}):
            host = RuntimeHost(_boot(root))
            first = host.open_conversation(
                ConversationRequest(agent_id="Pickle", cwd=root)
            )
            store = first.persistence_store
            now = datetime(2026, 8, 25, tzinfo=timezone.utc)
            message = store.send_message(
                message_id="message-1",
                session_id=first.session.session_id,
                delivery="followup",
                message=UserMessage((TextBlock("resume"),)),
                source=UserMessageSource(),
                created_at=now,
            )
            accepted = OperationService(store).accept_pending_message(
                message=message,
                agent_package_version_id=(first.agent_definition.package_version_id),
                workspace_binding=WorkspaceBinding(
                    workspace_id=first.session.workspace_id,
                    working_directory=first.session.cwd,
                    allowed_root=first.session.cwd,
                ),
                expected_node_id=first.session.active_node_id,
            )
            assert accepted is not None
            error = PackageLoadError(
                "extension_unavailable",
                accepted.operation.agent_package_version_id,
                "missing",
            )
            with patch.object(host.boot, "load_agent_package", side_effect=error):
                reopened = host.open_conversation(
                    ConversationRequest(session_id=first.session.session_id)
                )

        assert reopened.session.session_id == first.session.session_id
