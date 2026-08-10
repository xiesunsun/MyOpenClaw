from __future__ import annotations

import os
import textwrap
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from pickel.app.boot import Boot
from pickel.app.runtime_host import RuntimeHost
from pickel.app.runtime_models import ConversationRequest
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

        assert resumed.session.session_id == session_id
        assert resumed.agent.agent_id == "Pickle"
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
