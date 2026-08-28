from pathlib import Path
from tempfile import TemporaryDirectory
import textwrap
import unittest
from unittest.mock import patch

from pickel.shared.model_config import ModelConfig
from tests.helpers.yaml_app_config import app_config_from_yaml_file


class AppConfigTests(unittest.TestCase):
    def test_load_defaults_react_max_steps_to_eight(self) -> None:
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = root / "config.yaml"
            config_path.write_text(textwrap.dedent("""
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
                    """).strip())

            config = app_config_from_yaml_file(config_path)

            self.assertEqual(8, config.react_max_steps)

    def test_load_does_not_expose_legacy_context_cli_turn_window(self) -> None:
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = root / "config.yaml"
            config_path.write_text(textwrap.dedent("""
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
                    """).strip())

            config = app_config_from_yaml_file(config_path)

            self.assertFalse(hasattr(config, "context_cli_turn_window"))

    def test_extensions_section_passes_through_raw_and_extension_parses_defaults(
        self,
    ) -> None:
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = root / "config.yaml"
            config_path.write_text(textwrap.dedent("""
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
                    extensions:
                      openviking:
                        enabled: true
                        base_url: https://openviking.example
                        account_id: account
                        user_id: user
                        user_key: secret
                    """).strip())

            config = app_config_from_yaml_file(config_path)

            # core 只存原始 dict，不解析
            section = config.extensions["openviking"]
            self.assertTrue(section["enabled"])
            # 默认值由 extension 自己的模型给出
            from pickel.extensions.openviking.config import OpenVikingConfig

            parsed = OpenVikingConfig.model_validate(section)
            self.assertTrue(parsed.session_recall.enabled)
            self.assertEqual(6000, parsed.session_recall.max_chars)
            self.assertEqual(5, parsed.session_recall.limit)

    def test_load_resolves_agent_paths_relative_to_config_file(self) -> None:
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "agents" / "Pickle").mkdir(parents=True)
            (root / "agents" / "Pickle" / "AGENT.md").write_text("You are Pickle.\n")
            (root / "workspace").mkdir()
            config_path = root / "config.yaml"
            config_path.write_text(textwrap.dedent("""
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
                    """).strip())

            config = app_config_from_yaml_file(config_path)
            agent_config = config.get_agent_config()

            self.assertEqual(root / "workspace", agent_config.workspace_path)
            self.assertEqual(root / "agents" / "Pickle", agent_config.behavior_path)

    def test_load_reads_top_level_react_max_steps(self) -> None:
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = root / "config.yaml"
            config_path.write_text(textwrap.dedent("""
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
                    """).strip())

            config = app_config_from_yaml_file(config_path)

            self.assertEqual(16, config.react_max_steps)

    def test_load_ignores_legacy_top_level_context_cli_turn_window(self) -> None:
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = root / "config.yaml"
            config_path.write_text(textwrap.dedent("""
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
                    """).strip())

            config = app_config_from_yaml_file(config_path)

            self.assertFalse(hasattr(config, "context_cli_turn_window"))

    def test_resolve_model_config_merges_selected_provider_and_model(self) -> None:
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = root / "config.yaml"
            config_path.write_text(textwrap.dedent("""
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
                    """).strip())

            config = app_config_from_yaml_file(config_path)
            model_config = config.resolve_model_config()

            self.assertEqual("google/gemini", model_config.provider)
            self.assertEqual("gemini-3-flash-preview", model_config.model)
            self.assertEqual(0.2, model_config.temperature)

    def test_resolve_model_config_includes_max_input_tokens(self) -> None:
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = root / "config.yaml"
            config_path.write_text(textwrap.dedent("""
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
                    """).strip())

            config = app_config_from_yaml_file(config_path)
            model_config = config.resolve_model_config()

            self.assertEqual(1048576, model_config.max_input_tokens)

    def test_resolve_model_config_keeps_total_context_separate_from_input_limit(
        self,
    ) -> None:
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = root / "config.yaml"
            config_path.write_text(textwrap.dedent("""
                    default_agent: Pickle
                    default_llm:
                      provider: opencode-go
                      model: glm-5.3-flash
                    providers:
                      opencode-go:
                        models:
                          glm-5.3-flash:
                            api_key: test-key
                            wire_protocol: openai-chat-completions
                            context_window_tokens: 1000000
                            max_output_tokens: 65536
                            provider_options: {}
                    agents:
                      Pickle:
                        workspace_path: workspace
                        behavior_path: agents/Pickle
                    """).strip())

            config = app_config_from_yaml_file(config_path)
            model_config = config.resolve_model_config()

            assert model_config.context_window_tokens == 1000000
            assert model_config.max_input_tokens is None
            assert model_config.effect_rate == 0.5
            assert model_config.effective_input_token_limit(65536) == 434464

    def test_context_window_must_exceed_output_budget(self) -> None:
        with self.assertRaisesRegex(
            ValueError, "context_window_tokens.*max_output_tokens"
        ):
            ModelConfig(
                provider="anthropic",
                model="claude-test",
                wire_protocol="anthropic-messages",
                max_output_tokens=1024,
                context_window_tokens=1024,
            )

    def test_effective_input_limit_applies_requested_reserve_and_independent_cap(
        self,
    ) -> None:
        model = ModelConfig(
            provider="anthropic",
            model="claude-test",
            wire_protocol="anthropic-messages",
            max_input_tokens=700_000,
            max_output_tokens=65_536,
            context_window_tokens=1_000_000,
        )

        assert model.effective_input_token_limit() == 434_464
        assert model.effective_input_token_limit(400_000) == 100_000

    def test_effect_rate_is_configurable_and_validated(self) -> None:
        model = ModelConfig(
            provider="anthropic",
            model="claude-test",
            wire_protocol="anthropic-messages",
            max_output_tokens=10_000,
            context_window_tokens=100_000,
            effect_rate=0.75,
        )

        assert model.effective_input_token_limit() == 65_000
        for value in (0, -0.1, 1.1):
            with self.assertRaisesRegex(ValueError, "effect_rate"):
                ModelConfig(
                    provider="anthropic",
                    model="claude-test",
                    wire_protocol="anthropic-messages",
                    context_window_tokens=100_000,
                    effect_rate=value,
                )

    def test_resolve_model_config_defaults_temperature_to_none_when_omitted(
        self,
    ) -> None:
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = root / "config.yaml"
            config_path.write_text(textwrap.dedent("""
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
                    """).strip())

            config = app_config_from_yaml_file(config_path)
            model_config = config.resolve_model_config()

            self.assertIsNone(model_config.temperature)

    def test_resolve_model_config_reads_provider_options_thinking(self) -> None:
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = root / "config.yaml"
            config_path.write_text(textwrap.dedent("""
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
                    """).strip())

            config = app_config_from_yaml_file(config_path)
            model_config = config.resolve_model_config()

            self.assertEqual("xhigh", model_config.provider_options["thinking"])

    def test_load_expands_environment_variables_in_model_config(self) -> None:
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = root / "config.yaml"
            config_path.write_text(textwrap.dedent("""
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
                    """).strip())

            with patch.dict(
                "os.environ",
                {
                    "TEST_GEMINI_API_KEY": "secret-key",
                    "TEST_GEMINI_API_BASE": "https://example.com",
                },
                clear=False,
            ):
                config = app_config_from_yaml_file(config_path)

            model_config = config.resolve_model_config()
            self.assertEqual("secret-key", model_config.api_key)
            self.assertEqual("https://example.com", model_config.api_base)

    def test_load_raises_for_missing_environment_variable(self) -> None:
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = root / "config.yaml"
            config_path.write_text(textwrap.dedent("""
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
                    """).strip())

            with patch.dict("os.environ", {}, clear=True):
                with self.assertRaisesRegex(
                    ValueError, "Environment variable 'MISSING_API_KEY' is not set"
                ):
                    app_config_from_yaml_file(config_path)

    def test_file_access_mode_defaults_to_workspace(self) -> None:
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = root / "config.yaml"
            config_path.write_text(textwrap.dedent("""
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
                    """).strip())

            config = app_config_from_yaml_file(config_path)

            self.assertEqual("workspace", config.resolve_file_access_mode().value)

    def test_agent_file_access_mode_overrides_default(self) -> None:
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = root / "config.yaml"
            config_path.write_text(textwrap.dedent("""
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
                    """).strip())

            config = app_config_from_yaml_file(config_path)

            self.assertEqual("full", config.resolve_file_access_mode().value)

    def test_load_resolves_default_skills_path_relative_to_config_file(self) -> None:
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = root / "config.yaml"
            config_path.write_text(textwrap.dedent("""
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
                    """).strip())

            config = app_config_from_yaml_file(config_path)

            self.assertEqual(root / ".agent" / "skills", config.resolve_skills_path())

    def test_agent_skills_path_overrides_default(self) -> None:
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = root / "config.yaml"
            config_path.write_text(textwrap.dedent("""
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
                    """).strip())

            config = app_config_from_yaml_file(config_path)

            self.assertEqual(root / "custom-skills", config.resolve_skills_path())

    def test_opencode_go_requires_key_only_when_selected(self) -> None:
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = root / "config.yaml"
            config_path.write_text(textwrap.dedent("""
                    default_agent: Pickle
                    default_llm:
                      provider: opencode-go
                      model: kimi-k3
                    providers:
                      opencode-go:
                        models:
                          kimi-k3:
                            wire_protocol: openai-chat-completions
                    agents:
                      Pickle:
                        workspace_path: .
                        behavior_path: .
                    """).strip())
            config = app_config_from_yaml_file(config_path)

            with self.assertRaisesRegex(ValueError, "OpenCode Go 需要"):
                config.resolve_model_config()


if __name__ == "__main__":
    unittest.main()
