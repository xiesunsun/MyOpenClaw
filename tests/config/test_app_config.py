from pathlib import Path
from tempfile import TemporaryDirectory
import textwrap
import unittest

from myopenclaw.config.app_config import AppConfig


class AppConfigTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
