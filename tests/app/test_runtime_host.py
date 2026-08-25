from __future__ import annotations

import os
import textwrap
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from pickel.agents.agent_package_loader import PackageLoadError
from pickel.app.boot import Boot
from pickel.app.runtime_host import RuntimeHost
from pickel.app.runtime_models import ConversationRequest
from pickel.conversations.agent_message import UserMessage
from pickel.conversations.content_blocks import TextBlock
from pickel.inbox.message import UserMessageSource
from pickel.operations.operation_service import OperationService
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
