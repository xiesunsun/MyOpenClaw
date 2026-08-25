from __future__ import annotations

import os
import textwrap
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from pickel.app.boot import Boot
from tests.helpers.yaml_app_config import app_config_from_yaml_file


def test_loaded_package_captures_current_pickel_settings() -> None:
    with TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
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
                react_max_steps: 12
                context_cli_turn_window: 6
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

        with patch.dict(os.environ, {"PICKEL_HOME": str(root / "home")}):
            loaded = Boot.from_config(
                app_config_from_yaml_file(config_path)
            ).resolve_loaded_agent_package()

        version = loaded.version
        assert version.runtime_policy.max_model_steps == 12
        assert version.runtime_policy.context_turn_window == 6
        assert version.model_policy.primary.provider == "anthropic"
