from pathlib import Path
from tempfile import TemporaryDirectory
import textwrap
import unittest
from unittest.mock import patch

from myopenclaw.config.app_config import AppConfig


class AppConfigTests(unittest.TestCase):
    def test_load_defaults_react_max_steps_to_eight(self) -> None:
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = root / "config.yaml"
            config_path.write_text(
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
                            temperature: 0.2
                            max_output_tokens: 1024
                            provider_options: {}
                    agents:
                      Pickle:
                        workspace_path: workspace
                        behavior_path: agents/Pickle
                    """
                ).strip()
            )

            config = AppConfig.load(config_path)

            self.assertEqual(8, config.react_max_steps)

    def test_load_defaults_context_cli_turn_window_to_five(self) -> None:
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = root / "config.yaml"
            config_path.write_text(
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
                            temperature: 0.2
                            max_output_tokens: 1024
                            provider_options: {}
                    agents:
                      Pickle:
                        workspace_path: workspace
                        behavior_path: agents/Pickle
                    """
                ).strip()
            )

            config = AppConfig.load(config_path)

            self.assertEqual(5, config.context_cli_turn_window)

    def test_load_defaults_openviking_session_recall(self) -> None:
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = root / "config.yaml"
            config_path.write_text(
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
                            temperature: 0.2
                            max_output_tokens: 1024
                            provider_options: {}
                    agents:
                      Pickle:
                        workspace_path: workspace
                        behavior_path: agents/Pickle
                        remote_agent_id: remote-pickle
                    openviking:
                      enabled: true
                      base_url: https://openviking.example
                      account_id: account
                      user_id: user
                      user_key: secret
                    """
                ).strip()
            )

            config = AppConfig.load(config_path)

            self.assertIsNotNone(config.openviking)
            assert config.openviking is not None
            self.assertTrue(config.openviking.session_recall.enabled)
            self.assertEqual(6000, config.openviking.session_recall.max_chars)
            self.assertEqual(5, config.openviking.session_recall.limit)

    def test_load_resolves_agent_paths_relative_to_config_file(self) -> None:
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "agents" / "Pickle").mkdir(parents=True)
            (root / "agents" / "Pickle" / "AGENT.md").write_text("You are Pickle.\n")
            (root / "workspace").mkdir()
            config_path = root / "config.yaml"
            config_path.write_text(
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
                            temperature: 1.0
                            max_output_tokens: 1024
                            provider_options: {}
                    agents:
                      Pickle:
                        workspace_path: workspace
                        behavior_path: agents/Pickle
                        tools:
                          - echo
                    """
                ).strip()
            )

            config = AppConfig.load(config_path)
            agent_config = config.get_agent_config()

            self.assertEqual(root / "workspace", agent_config.workspace_path)
            self.assertEqual(root / "agents" / "Pickle", agent_config.behavior_path)

    def test_load_reads_top_level_react_max_steps(self) -> None:
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = root / "config.yaml"
            config_path.write_text(
                textwrap.dedent(
                    """
                    default_agent: Pickle
                    react_max_steps: 16
                    default_llm:
                      provider: google/gemini
                      model: gemini-3-flash-preview
                    providers:
                      google/gemini:
                        models:
                          gemini-3-flash-preview:
                            temperature: 0.2
                            max_output_tokens: 1024
                            provider_options: {}
                    agents:
                      Pickle:
                        workspace_path: workspace
                        behavior_path: agents/Pickle
                    """
                ).strip()
            )

            config = AppConfig.load(config_path)

            self.assertEqual(16, config.react_max_steps)

    def test_load_reads_top_level_context_cli_turn_window(self) -> None:
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = root / "config.yaml"
            config_path.write_text(
                textwrap.dedent(
                    """
                    default_agent: Pickle
                    context_cli_turn_window: 9
                    default_llm:
                      provider: google/gemini
                      model: gemini-3-flash-preview
                    providers:
                      google/gemini:
                        models:
                          gemini-3-flash-preview:
                            temperature: 0.2
                            max_output_tokens: 1024
                            provider_options: {}
                    agents:
                      Pickle:
                        workspace_path: workspace
                        behavior_path: agents/Pickle
                    """
                ).strip()
            )

            config = AppConfig.load(config_path)

            self.assertEqual(9, config.context_cli_turn_window)

    def test_resolve_model_config_merges_selected_provider_and_model(self) -> None:
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = root / "config.yaml"
            config_path.write_text(
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
                            temperature: 0.2
                            max_output_tokens: 1024
                            provider_options: {}
                    agents:
                      Pickle:
                        workspace_path: workspace
                        behavior_path: agents/Pickle
                    """
                ).strip()
            )

            config = AppConfig.load(config_path)
            model_config = config.resolve_model_config()

            self.assertEqual("google/gemini", model_config.provider)
            self.assertEqual("gemini-3-flash-preview", model_config.model)
            self.assertEqual(0.2, model_config.temperature)

    def test_resolve_model_config_includes_max_input_tokens(self) -> None:
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = root / "config.yaml"
            config_path.write_text(
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
                            temperature: 0.2
                            max_input_tokens: 1048576
                            max_output_tokens: 1024
                            provider_options: {}
                    agents:
                      Pickle:
                        workspace_path: workspace
                        behavior_path: agents/Pickle
                    """
                ).strip()
            )

            config = AppConfig.load(config_path)
            model_config = config.resolve_model_config()

            self.assertEqual(1048576, model_config.max_input_tokens)

    def test_resolve_model_config_defaults_temperature_to_none_when_omitted(self) -> None:
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = root / "config.yaml"
            config_path.write_text(
                textwrap.dedent(
                    """
                    default_agent: Pickle
                    default_llm:
                      provider: anthropic
                      model: claude-opus-4-7
                    providers:
                      anthropic:
                        models:
                          claude-opus-4-7:
                            max_output_tokens: 1024
                            provider_options: {}
                    agents:
                      Pickle:
                        workspace_path: workspace
                        behavior_path: agents/Pickle
                    """
                ).strip()
            )

            config = AppConfig.load(config_path)
            model_config = config.resolve_model_config()

            self.assertIsNone(model_config.temperature)

    def test_resolve_model_config_reads_provider_options_thinking(self) -> None:
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = root / "config.yaml"
            config_path.write_text(
                textwrap.dedent(
                    """
                    default_agent: Pickle
                    default_llm:
                      provider: anthropic
                      model: claude-opus-4-7
                    providers:
                      anthropic:
                        models:
                          claude-opus-4-7:
                            max_output_tokens: 1024
                            provider_options:
                              thinking: xhigh
                    agents:
                      Pickle:
                        workspace_path: workspace
                        behavior_path: agents/Pickle
                    """
                ).strip()
            )

            config = AppConfig.load(config_path)
            model_config = config.resolve_model_config()

            self.assertEqual("xhigh", model_config.provider_options["thinking"])

    def test_load_expands_environment_variables_in_model_config(self) -> None:
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = root / "config.yaml"
            config_path.write_text(
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
                            api_key: ${TEST_GEMINI_API_KEY}
                            api_base: ${TEST_GEMINI_API_BASE}
                            temperature: 0.2
                            max_output_tokens: 1024
                            provider_options: {}
                    agents:
                      Pickle:
                        workspace_path: workspace
                        behavior_path: agents/Pickle
                    """
                ).strip()
            )

            with patch.dict(
                "os.environ",
                {
                    "TEST_GEMINI_API_KEY": "secret-key",
                    "TEST_GEMINI_API_BASE": "https://example.com",
                },
                clear=False,
            ):
                config = AppConfig.load(config_path)

            model_config = config.resolve_model_config()
            self.assertEqual("secret-key", model_config.api_key)
            self.assertEqual("https://example.com", model_config.api_base)

    def test_load_raises_for_missing_environment_variable(self) -> None:
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = root / "config.yaml"
            config_path.write_text(
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
                            api_key: ${MISSING_API_KEY}
                            temperature: 0.2
                            max_output_tokens: 1024
                            provider_options: {}
                    agents:
                      Pickle:
                        workspace_path: workspace
                        behavior_path: agents/Pickle
                    """
                ).strip()
            )

            with patch.dict("os.environ", {}, clear=True):
                with self.assertRaisesRegex(
                    ValueError, "Environment variable 'MISSING_API_KEY' is not set"
                ):
                    AppConfig.load(config_path)

    def test_file_access_mode_defaults_to_workspace(self) -> None:
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = root / "config.yaml"
            config_path.write_text(
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
                            temperature: 0.2
                            max_output_tokens: 1024
                            provider_options: {}
                    agents:
                      Pickle:
                        workspace_path: workspace
                        behavior_path: agents/Pickle
                    """
                ).strip()
            )

            config = AppConfig.load(config_path)

            self.assertEqual("workspace", config.resolve_file_access_mode().value)

    def test_agent_file_access_mode_overrides_default(self) -> None:
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = root / "config.yaml"
            config_path.write_text(
                textwrap.dedent(
                    """
                    default_agent: Pickle
                    default_file_access_mode: workspace
                    default_llm:
                      provider: google/gemini
                      model: gemini-3-flash-preview
                    providers:
                      google/gemini:
                        models:
                          gemini-3-flash-preview:
                            temperature: 0.2
                            max_output_tokens: 1024
                            provider_options: {}
                    agents:
                      Pickle:
                        workspace_path: workspace
                        behavior_path: agents/Pickle
                        file_access_mode: full
                    """
                ).strip()
            )

            config = AppConfig.load(config_path)

            self.assertEqual("full", config.resolve_file_access_mode().value)

    def test_load_resolves_default_skills_path_relative_to_config_file(self) -> None:
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = root / "config.yaml"
            config_path.write_text(
                textwrap.dedent(
                    """
                    default_agent: Pickle
                    default_skills_path: .agent/skills
                    default_llm:
                      provider: google/gemini
                      model: gemini-3-flash-preview
                    providers:
                      google/gemini:
                        models:
                          gemini-3-flash-preview:
                            temperature: 0.2
                            max_output_tokens: 1024
                            provider_options: {}
                    agents:
                      Pickle:
                        workspace_path: workspace
                        behavior_path: agents/Pickle
                    """
                ).strip()
            )

            config = AppConfig.load(config_path)

            self.assertEqual(root / ".agent" / "skills", config.resolve_skills_path())

    def test_agent_skills_path_overrides_default(self) -> None:
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = root / "config.yaml"
            config_path.write_text(
                textwrap.dedent(
                    """
                    default_agent: Pickle
                    default_skills_path: .agent/skills
                    default_llm:
                      provider: google/gemini
                      model: gemini-3-flash-preview
                    providers:
                      google/gemini:
                        models:
                          gemini-3-flash-preview:
                            temperature: 0.2
                            max_output_tokens: 1024
                            provider_options: {}
                    agents:
                      Pickle:
                        workspace_path: workspace
                        behavior_path: agents/Pickle
                        skills_path: custom-skills
                    """
                ).strip()
            )

            config = AppConfig.load(config_path)

            self.assertEqual(root / "custom-skills", config.resolve_skills_path())


if __name__ == "__main__":
    unittest.main()
