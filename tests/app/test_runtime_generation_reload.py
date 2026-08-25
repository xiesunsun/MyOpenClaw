from __future__ import annotations

import asyncio
import os
import textwrap
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from pickel.app.boot import Boot
from pickel.app.runtime_host import RuntimeHost
from pickel.app.runtime_models import ConversationRequest
from pickel.extensions_host.loader import LoadResult
from pickel.app.runtime_generation import RuntimeGeneration, RuntimeGenerationState
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
            second = host.open_conversation(
                ConversationRequest(agent_id="Pickle", cwd=root)
            )
            old_generation = host.active_generation
            host._attach = Mock(return_value=Mock())
            asyncio.run(host.reload(first, app_config=host.app_config))

            asyncio.run(host.shutdown())

        assert old_generation.state is RuntimeGenerationState.CLOSED
