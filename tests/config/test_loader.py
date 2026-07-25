"""Config 分层加载：settings / models / auth 合并为 AppConfig。"""

from __future__ import annotations

import json
import os
import textwrap
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from pickel.config.loader import Config


class ConfigLoaderTests(unittest.TestCase):
    def _write_json(self, path: Path, data: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")

    def _minimal_models(self) -> dict:
        return {
            "providers": {
                "google/gemini": {
                    "models": {
                        "gemini-3-flash-preview": {
                            "temperature": 0.2,
                            "max_output_tokens": 1024,
                            "provider_options": {},
                        }
                    }
                }
            }
        }

    def _minimal_settings(self, **overrides: object) -> dict:
        data: dict = {
            "default_agent": "Pickle",
            "default_llm": {
                "provider": "google/gemini",
                "model": "gemini-3-flash-preview",
            },
        }
        data.update(overrides)
        return data

    def test_load_merges_global_settings_models_auth(self) -> None:
        with TemporaryDirectory() as tmpdir:
            home = Path(tmpdir) / "home"
            project = Path(tmpdir) / "project"
            (project / ".pickel").mkdir(parents=True)

            self._write_json(
                home / "settings.json",
                self._minimal_settings(react_max_steps=16),
            )
            self._write_json(home / "models.json", self._minimal_models())
            self._write_json(
                home / "auth.json",
                {
                    "providers": {
                        "google/gemini": {
                            "api_key": "global-key",
                            "api_base": "https://example.com/v1",
                        }
                    }
                },
            )

            with patch.dict(os.environ, {"PICKEL_HOME": str(home)}):
                config = Config.load(cwd=project, home=home)

            self.assertEqual("Pickle", config.default_agent)
            self.assertEqual(16, config.react_max_steps)
            model = config.resolve_model_config()
            self.assertEqual("global-key", model.api_key)
            self.assertEqual("https://example.com/v1", model.api_base)
            self.assertEqual(0.2, model.temperature)
            self.assertEqual(project.resolve(), config.root)

    def test_project_settings_override_global(self) -> None:
        with TemporaryDirectory() as tmpdir:
            home = Path(tmpdir) / "home"
            project = Path(tmpdir) / "project"
            pickel = project / ".pickel"
            pickel.mkdir(parents=True)

            self._write_json(
                home / "settings.json",
                self._minimal_settings(
                    default_agent="GlobalAgent",
                    react_max_steps=8,
                ),
            )
            self._write_json(home / "models.json", self._minimal_models())
            self._write_json(home / "auth.json", {"providers": {}})

            self._write_json(
                pickel / "settings.json",
                {"default_agent": "ProjectAgent", "react_max_steps": 20},
            )

            config = Config.load(cwd=project, home=home)

            self.assertEqual("ProjectAgent", config.default_agent)
            self.assertEqual(20, config.react_max_steps)

    def test_project_models_override_global_model_fields(self) -> None:
        with TemporaryDirectory() as tmpdir:
            home = Path(tmpdir) / "home"
            project = Path(tmpdir) / "project"
            pickel = project / ".pickel"
            pickel.mkdir(parents=True)

            self._write_json(home / "settings.json", self._minimal_settings())
            self._write_json(home / "models.json", self._minimal_models())
            self._write_json(home / "auth.json", {"providers": {}})
            self._write_json(
                pickel / "models.json",
                {
                    "providers": {
                        "google/gemini": {
                            "models": {
                                "gemini-3-flash-preview": {
                                    "temperature": 0.9,
                                }
                            }
                        }
                    }
                },
            )

            config = Config.load(cwd=project, home=home)
            model = config.resolve_model_config()

            self.assertEqual(0.9, model.temperature)
            self.assertEqual(1024, model.max_output_tokens)

    def test_expand_env_vars_in_auth(self) -> None:
        with TemporaryDirectory() as tmpdir:
            home = Path(tmpdir) / "home"
            project = Path(tmpdir) / "project"
            (project / ".pickel").mkdir(parents=True)

            self._write_json(home / "settings.json", self._minimal_settings())
            self._write_json(home / "models.json", self._minimal_models())
            self._write_json(
                home / "auth.json",
                {
                    "providers": {
                        "google/gemini": {
                            "api_key": "${TEST_LOADER_API_KEY}",
                            "api_base": "${TEST_LOADER_API_BASE}",
                        }
                    }
                },
            )

            with patch.dict(
                os.environ,
                {
                    "TEST_LOADER_API_KEY": "from-env",
                    "TEST_LOADER_API_BASE": "https://env.example.com",
                },
                clear=False,
            ):
                config = Config.load(cwd=project, home=home)

            model = config.resolve_model_config()
            self.assertEqual("from-env", model.api_key)
            self.assertEqual("https://env.example.com", model.api_base)

    def test_missing_env_var_raises(self) -> None:
        with TemporaryDirectory() as tmpdir:
            home = Path(tmpdir) / "home"
            project = Path(tmpdir) / "project"
            (project / ".pickel").mkdir(parents=True)

            self._write_json(home / "settings.json", self._minimal_settings())
            self._write_json(home / "models.json", self._minimal_models())
            self._write_json(
                home / "auth.json",
                {
                    "providers": {
                        "google/gemini": {
                            "api_key": "${MISSING_LOADER_KEY}",
                        }
                    }
                },
            )

            with patch.dict(os.environ, {}, clear=True):
                with self.assertRaisesRegex(
                    ValueError, "Environment variable 'MISSING_LOADER_KEY' is not set"
                ):
                    Config.load(cwd=project, home=home)

    def test_auth_fills_api_key_when_model_lacks_it(self) -> None:
        with TemporaryDirectory() as tmpdir:
            home = Path(tmpdir) / "home"
            project = Path(tmpdir) / "project"
            (project / ".pickel").mkdir(parents=True)

            self._write_json(home / "settings.json", self._minimal_settings())
            models = self._minimal_models()
            models["providers"]["google/gemini"]["models"]["gemini-3-flash-preview"][
                "api_key"
            ] = "model-key"
            self._write_json(home / "models.json", models)
            self._write_json(
                home / "auth.json",
                {
                    "providers": {
                        "google/gemini": {
                            "api_key": "auth-key",
                            "api_base": "https://auth.example.com",
                        }
                    }
                },
            )

            config = Config.load(cwd=project, home=home)
            model = config.resolve_model_config()

            # 模型已有 api_key 时保留；缺 api_base 时用 auth
            self.assertEqual("model-key", model.api_key)
            self.assertEqual("https://auth.example.com", model.api_base)

    def test_openviking_merges_settings_strategy_and_auth_secrets(self) -> None:
        with TemporaryDirectory() as tmpdir:
            home = Path(tmpdir) / "home"
            project = Path(tmpdir) / "project"
            (project / ".pickel").mkdir(parents=True)

            self._write_json(
                home / "settings.json",
                self._minimal_settings(
                    openviking={
                        "enabled": True,
                        "timeout_seconds": 45,
                        "session_recall": {"enabled": False, "max_chars": 1000, "limit": 2},
                    }
                ),
            )
            self._write_json(home / "models.json", self._minimal_models())
            self._write_json(
                home / "auth.json",
                {
                    "providers": {},
                    "openviking": {
                        "base_url": "https://ov.example",
                        "account_id": "acc",
                        "user_id": "user",
                        "user_key": "secret",
                    },
                },
            )

            config = Config.load(cwd=project, home=home)

            self.assertIsNotNone(config.openviking)
            assert config.openviking is not None
            self.assertTrue(config.openviking.enabled)
            self.assertEqual(45.0, config.openviking.timeout_seconds)
            self.assertEqual("https://ov.example", config.openviking.base_url)
            self.assertEqual("secret", config.openviking.user_key)
            self.assertFalse(config.openviking.session_recall.enabled)
            self.assertEqual(1000, config.openviking.session_recall.max_chars)

    def test_legacy_config_yaml_supplies_defaults_when_settings_missing(self) -> None:
        """未 migrate 时：仅有 config.yaml 也应能 Config.load（default_agent/llm）。"""
        with TemporaryDirectory() as tmpdir:
            home = Path(tmpdir) / "home"
            home.mkdir()
            project = Path(tmpdir) / "project"
            (project / "agents" / "Pickle").mkdir(parents=True)
            (project / "agents" / "Pickle" / "AGENT.md").write_text("hi", encoding="utf-8")
            (project / "config.yaml").write_text(
                textwrap.dedent(
                    """
                    default_agent: Pickle
                    default_llm:
                      provider: google/gemini
                      model: gemini-3-flash-preview
                    providers:
                      google/gemini:
                        models:
                          gemini-3-flash-preview:
                            max_output_tokens: 1024
                    agents:
                      Pickle:
                        workspace_path: workspace
                        behavior_path: agents/Pickle
                    """
                ).strip(),
                encoding="utf-8",
            )
            (project / "workspace").mkdir()

            with patch.dict(os.environ, {"PICKEL_HOME": str(home)}, clear=False):
                config = Config.load(cwd=project, home=home)

            self.assertEqual("Pickle", config.default_agent)
            self.assertEqual("google/gemini", config.default_llm.provider)
            self.assertIn("Pickle", config.agents)

    def test_legacy_config_yaml_agents_merged_when_present(self) -> None:
        with TemporaryDirectory() as tmpdir:
            home = Path(tmpdir) / "home"
            project = Path(tmpdir) / "project"
            (project / ".pickel").mkdir(parents=True)
            (project / "workspace").mkdir()
            (project / "agents" / "Pickle").mkdir(parents=True)

            self._write_json(home / "settings.json", self._minimal_settings())
            self._write_json(home / "models.json", self._minimal_models())
            self._write_json(home / "auth.json", {"providers": {}})

            (project / "config.yaml").write_text(
                textwrap.dedent(
                    """
                    agents:
                      Pickle:
                        workspace_path: workspace
                        behavior_path: agents/Pickle
                        tools:
                          - echo
                    """
                ).strip(),
                encoding="utf-8",
            )

            config = Config.load(cwd=project, home=home)
            agent = config.get_agent_config("Pickle")

            self.assertEqual(project.resolve() / "workspace", agent.workspace_path)
            self.assertEqual(project.resolve() / "agents" / "Pickle", agent.behavior_path)
            self.assertEqual(["echo"], agent.tools)

    def test_builtin_defaults_apply_when_settings_omit_optional_fields(self) -> None:
        with TemporaryDirectory() as tmpdir:
            home = Path(tmpdir) / "home"
            project = Path(tmpdir) / "project"
            (project / ".pickel").mkdir(parents=True)

            self._write_json(home / "settings.json", self._minimal_settings())
            self._write_json(home / "models.json", self._minimal_models())
            self._write_json(home / "auth.json", {"providers": {}})

            config = Config.load(cwd=project, home=home)

            self.assertEqual(8, config.react_max_steps)
            self.assertEqual(5, config.context_cli_turn_window)
            self.assertEqual("workspace", config.default_file_access_mode.value)

    def test_home_defaults_to_pickel_home(self) -> None:
        with TemporaryDirectory() as tmpdir:
            home = Path(tmpdir) / "home"
            project = Path(tmpdir) / "project"
            (project / ".pickel").mkdir(parents=True)

            self._write_json(home / "settings.json", self._minimal_settings())
            self._write_json(home / "models.json", self._minimal_models())
            self._write_json(home / "auth.json", {"providers": {}})

            with patch.dict(os.environ, {"PICKEL_HOME": str(home)}):
                config = Config.load(cwd=project)

            self.assertEqual("Pickle", config.default_agent)

    def test_agents_empty_when_no_legacy_yaml(self) -> None:
        with TemporaryDirectory() as tmpdir:
            home = Path(tmpdir) / "home"
            project = Path(tmpdir) / "project"
            (project / ".pickel").mkdir(parents=True)

            self._write_json(home / "settings.json", self._minimal_settings())
            self._write_json(home / "models.json", self._minimal_models())
            self._write_json(home / "auth.json", {"providers": {}})

            config = Config.load(cwd=project, home=home)

            self.assertEqual({}, config.agents)


if __name__ == "__main__":
    unittest.main()
