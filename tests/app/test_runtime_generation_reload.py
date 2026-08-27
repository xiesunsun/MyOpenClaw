from __future__ import annotations

import asyncio
import os
import textwrap
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from pickel.app.boot import Boot
from pickel.app.runtime_host import RuntimeHost
from pickel.app.runtime_models import ConversationRequest
from pickel.conversations.agent_message import AssistantMessage, UserMessage
from pickel.conversations.content_blocks import TextBlock, ToolCallBlock
from pickel.conversations.conversation_node import ConversationNode
from pickel.extensions_host.loader import LoadResult
from pickel.app.runtime_generation import RuntimeGeneration, RuntimeGenerationState
from pickel.inbox.message import UserMessageSource
from pickel.operations.agent_run_state import (
    ModelStepState,
    ToolApproval,
    ToolCallState,
)
from pickel.operations.operation_service import OperationService
from pickel.workspaces.workspace_binding import WorkspaceBinding
from tests.helpers.yaml_app_config import app_config_from_yaml_file


def _boot(root: Path) -> Boot:
    (root / "agents" / "Pickle").mkdir(parents=True)
    (root / "agents" / "Pickle" / "AGENT.md").write_text(
        "You are Pickle.\n", encoding="utf-8"
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


class _Scope:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def close(self) -> None:
        self.events.append("closed")


def test_reload_success_atomically_retires_and_closes_old_generation() -> None:
    with TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        with patch.dict(os.environ, {"PICKEL_HOME": str(root / "home")}):
            boot = _boot(root)
            events: list[str] = []
            boot.extension_result = LoadResult(
                hosts={
                    "old": SimpleNamespace(scope=_Scope(events), extension_version=None)
                },
                modules={"old": object()},
            )
            host = RuntimeHost(boot)
            conversation = host.open_conversation(
                ConversationRequest(agent_id="Pickle", cwd=root)
            )
            old_generation = host.active_generation
            replacement = Mock()
            host._attach = Mock(return_value=replacement)

            result = asyncio.run(host.reload(conversation, app_config=host.app_config))

        assert result.conversation is replacement
        assert host.active_generation is not old_generation
        assert old_generation.state is RuntimeGenerationState.CLOSED
        assert events == ["closed"]
        assert conversation.closed


def test_reload_build_failure_keeps_old_generation_serving() -> None:
    with TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        with patch.dict(os.environ, {"PICKEL_HOME": str(root / "home")}):
            host = RuntimeHost(_boot(root))
            conversation = host.open_conversation(
                ConversationRequest(agent_id="Pickle", cwd=root)
            )
            old_agent = host.agent_registry.get(conversation.session.session_id)
            old_generation = host.active_generation

            def fail_boot(*_args, **_kwargs):
                raise RuntimeError("new generation invalid")

            with pytest.raises(RuntimeError, match="new generation invalid"):
                asyncio.run(
                    host.reload(
                        conversation,
                        app_config=host.app_config,
                        boot_factory=fail_boot,
                    )
                )

        assert host.active_generation is old_generation
        assert old_generation.state is RuntimeGenerationState.ACTIVE
        assert not conversation.closed
        assert old_agent is not None
        assert host.agent_registry.get(conversation.session.session_id) is old_agent


def test_reload_replaces_agent_only_after_new_generation_attach_succeeds() -> None:
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

            result = asyncio.run(host.reload(conversation, app_config=host.app_config))

            new_agent = host.agent_registry.get(session_id)
            assert new_agent is not None
            assert new_agent is not old_agent
            assert result.conversation is not conversation
            assert host.agent_registry.get(session_id) is new_agent
            assert not host.agent_registry.unregister(session_id, old_agent)

        assert conversation.closed


def test_reload_does_not_wait_for_other_old_generation_conversations() -> None:
    with TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        with patch.dict(os.environ, {"PICKEL_HOME": str(root / "home")}):
            host = RuntimeHost(_boot(root))
            first = host.open_conversation(
                ConversationRequest(agent_id="Pickle", cwd=root)
            )
            second = host.open_conversation(
                ConversationRequest(agent_id="Pickle", cwd=root)
            )
            old_generation = host.active_generation
            host._attach = Mock(return_value=Mock())

            asyncio.run(host.reload(first, app_config=host.app_config))

        assert old_generation.state is RuntimeGenerationState.RETIRED
        assert old_generation.operation_ref_count == 1
        assert not second.closed


def test_inspect_mcp_uses_conversation_generation_catalog() -> None:
    class _Status:
        def snapshot(self):
            return SimpleNamespace(servers=(), diagnostics=())

    with TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        with patch.dict(os.environ, {"PICKEL_HOME": str(root / "home")}):
            host = RuntimeHost(_boot(root))
            conversation = host.open_conversation(
                ConversationRequest(agent_id="Pickle", cwd=root)
            )
            old_generation = conversation.runtime_generation
            assert old_generation is not None
            old_generation.extension_catalog = SimpleNamespace(
                mcp_status_source=_Status()
            )
            host._active_generation = RuntimeGeneration(
                "new-generation",
                state=RuntimeGenerationState.ACTIVE,
                extension_catalog=SimpleNamespace(mcp_status_source=None),
            )

            inspection = host.inspect_mcp(conversation)

        assert inspection.available


def test_shutdown_waits_for_retired_generation_after_reload() -> None:
    with TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        with patch.dict(os.environ, {"PICKEL_HOME": str(root / "home")}):
            host = RuntimeHost(_boot(root))
            first = host.open_conversation(
                ConversationRequest(agent_id="Pickle", cwd=root)
            )
            host.open_conversation(ConversationRequest(agent_id="Pickle", cwd=root))
            old_generation = host.active_generation
            host._attach = Mock(return_value=Mock())
            asyncio.run(host.reload(first, app_config=host.app_config))

            asyncio.run(host.shutdown())

        assert old_generation.state is RuntimeGenerationState.CLOSED


def test_waiting_operation_keeps_old_generation_until_terminal_after_reload() -> None:
    async def scenario(root: Path) -> None:
        host = RuntimeHost(_boot(root))
        conversation = host.open_conversation(
            ConversationRequest(agent_id="Pickle", cwd=root)
        )
        store = conversation.persistence_store
        now = datetime.now(timezone.utc)
        message = store.send_message(
            message_id="message-waiting",
            session_id=conversation.session.session_id,
            delivery="followup",
            message=UserMessage((TextBlock("wait"),)),
            source=UserMessageSource(),
            created_at=now,
        )
        accepted = OperationService(store).accept_pending_message(
            message=message,
            agent_package_version_id=conversation.agent_definition.package_version_id,
            workspace_binding=WorkspaceBinding(
                workspace_id=conversation.session.workspace_id,
                working_directory=conversation.session.cwd,
                allowed_root=conversation.session.cwd,
            ),
            expected_node_id=conversation.session.active_node_id,
        )
        assert accepted is not None
        call = ToolCallState(
            tool_call_id="tool-waiting",
            tool_name="approval_tool",
            arguments={},
            status="waiting_approval",
            approval=ToolApproval(
                requested_at=now,
                requested_by="hook",
                reason="test",
                decision=None,
            ),
            replay_policy="safe",
            execution_intent=None,
            decision_reason=None,
            result_node_id=None,
            is_error=None,
        )
        assistant_node = ConversationNode(
            node_id="assistant-waiting",
            session_id=conversation.session.session_id,
            parent_node_id=accepted.operation.input_node_id,
            content_type="agent_message",
            content=AssistantMessage(
                content=(
                    ToolCallBlock(
                        id=call.tool_call_id,
                        name=call.tool_name,
                        arguments=call.arguments,
                    ),
                )
            ),
            created_at=now,
        )
        waiting = replace(
            accepted.state,
            revision=accepted.state.revision + 1,
            status="waiting",
            waiting_reason="tool_approval",
            current_step=ModelStepState(
                step_id="step-waiting",
                step_sequence=1,
                phase="awaiting_tools",
                request_attempt=1,
                request_intent=None,
                assistant_message_node_id=assistant_node.node_id,
                tool_calls=(call,),
            ),
        )
        assert store.commit_run_transition(
            state=waiting,
            expected_revision=accepted.state.revision,
            node=assistant_node,
            updated_at=now,
        )

        old_generation = host.active_generation
        old_boot = host.boot
        result = await conversation._agent.resume_operation(
            accepted.operation.operation_id
        )
        assert result.status == "waiting"
        operation_handle = host._operation_package_handles[
            accepted.operation.operation_id
        ]
        assert operation_handle.generation is old_generation

        reloaded = await host.reload(conversation, app_config=host.app_config)
        assert old_generation.state is RuntimeGenerationState.RETIRED
        assert old_generation.operation_ref_count == 1

        new_agent = host.agent_registry.get(conversation.session.session_id)
        assert new_agent is not None
        new_boot = host.boot
        with (
            patch.object(
                old_boot, "_build_effects", wraps=old_boot._build_effects
            ) as old_effects,
            patch.object(
                new_boot, "_build_effects", wraps=new_boot._build_effects
            ) as new_effects,
        ):
            assert OperationService(store).request_cancellation(
                accepted.operation.operation_id,
                reason="test terminal",
            )
            terminal = await new_agent._driver._operation_driver.drive_operation(
                accepted.operation.operation_id
            )

        assert terminal.status == "cancelled"
        assert old_effects.call_count == 1
        assert new_effects.call_count == 0
        assert accepted.operation.operation_id not in host._operation_package_handles
        assert old_generation.state is RuntimeGenerationState.CLOSED
        assert old_generation not in host._retired_generations
        reloaded.conversation.detach()
        await host.shutdown()

    with TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        with patch.dict(os.environ, {"PICKEL_HOME": str(root / "home")}):
            asyncio.run(scenario(root))
