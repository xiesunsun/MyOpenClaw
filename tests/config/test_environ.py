"""Environ 进程覆盖单元测试。"""

from __future__ import annotations

import textwrap
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from pickel.config.environ import Environ
from pickel.shared.model_config import ModelSelection
from tests.helpers.yaml_app_config import app_config_from_yaml_file


def _write_multi_model_config(root: Path) -> Path:
    config_path = root / "config.yaml"
    config_path.write_text(
        textwrap.dedent("""
            default_agent: Pickle
            default_llm:
              provider: anthropic
              model: claude-opus-4-7
            providers:
              anthropic:
                models:
                  claude-opus-4-7:
                    api_key: test-key
                    max_output_tokens: 1024
                    provider_options:
                      thinking: low
                  claude-sonnet-4-6:
                    api_key: test-key
                    max_output_tokens: 2048
                    provider_options:
                      thinking: medium
              google/gemini:
                models:
                  gemini-3-flash-preview:
                    api_key: gemini-key
                    temperature: 0.2
                    max_output_tokens: 512
                    provider_options: {}
            agents:
              Pickle:
                workspace_path: workspace
                behavior_path: agents/Pickle
            """).strip(),
        encoding="utf-8",
    )
    return config_path


class EnvironTests(unittest.TestCase):
    def test_apply_to_selection_overrides_when_llm_set(self) -> None:
        base = ModelSelection(provider="anthropic", model="claude-opus-4-7")
        env = Environ(
            llm=ModelSelection(provider="google/gemini", model="gemini-3-flash-preview")
        )
        resolved = env.apply_to_selection(base)
        self.assertEqual("google/gemini", resolved.provider)
        self.assertEqual("gemini-3-flash-preview", resolved.model)

    def test_apply_to_selection_keeps_base_when_llm_none(self) -> None:
        base = ModelSelection(provider="anthropic", model="claude-opus-4-7")
        env = Environ()
        resolved = env.apply_to_selection(base)
        self.assertEqual(base, resolved)

    def test_merge_provider_options_environ_wins(self) -> None:
        env = Environ(provider_options={"thinking": "xhigh", "timeout_seconds": 30})
        merged = env.merge_provider_options({"thinking": "low", "max_retries": 2})
        self.assertEqual(
            {"thinking": "xhigh", "max_retries": 2, "timeout_seconds": 30},
            merged,
        )

    def test_resolve_model_config_environ_overrides_selection(self) -> None:
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = app_config_from_yaml_file(_write_multi_model_config(root))
            env = Environ(
                llm=ModelSelection(
                    provider="google/gemini", model="gemini-3-flash-preview"
                )
            )

            model = config.resolve_model_config(environ=env)

            self.assertEqual("google/gemini", model.provider)
            self.assertEqual("gemini-3-flash-preview", model.model)
            self.assertEqual(0.2, model.temperature)

    def test_resolve_model_config_environ_overrides_agent_selection(self) -> None:
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = app_config_from_yaml_file(_write_multi_model_config(root))
            agent_selection = ModelSelection(
                provider="anthropic", model="claude-opus-4-7"
            )
            env = Environ(
                llm=ModelSelection(provider="anthropic", model="claude-sonnet-4-6")
            )

            model = config.resolve_model_config(agent_selection, environ=env)

            self.assertEqual("anthropic", model.provider)
            self.assertEqual("claude-sonnet-4-6", model.model)
            self.assertEqual(2048, model.max_output_tokens)

    def test_resolve_model_config_merges_environ_provider_options(self) -> None:
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = app_config_from_yaml_file(_write_multi_model_config(root))
            env = Environ(provider_options={"thinking": "xhigh"})

            model = config.resolve_model_config(environ=env)

            self.assertEqual("anthropic", model.provider)
            self.assertEqual("claude-opus-4-7", model.model)
            self.assertEqual("xhigh", model.provider_options["thinking"])


if __name__ == "__main__":
    unittest.main()
