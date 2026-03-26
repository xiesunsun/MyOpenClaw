import tempfile
import textwrap
import unittest
from pathlib import Path
import sys

SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from myopenclaw.config.app_config import AppConfig


class AppConfigLoadTests(unittest.TestCase):
    def test_load_resolves_current_yaml_shape_into_agent_definition(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            behavior_dir = root / "agents" / "Pickle"
            behavior_dir.mkdir(parents=True)
            (behavior_dir / "AGENT.md").write_text(
                textwrap.dedent(
                    """
                    ---
                    name: Pickle
                    ---

                    You are Pickle.
                    """
                ).strip()
                + "\n",
                encoding="utf-8",
            )
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
                            max_output_tokens: 1048576
                            thinking_level: minimal
                            provider_options: {}

                    agents:
                      Pickle:
                        workspace_path: pickle_workspace
                        behavior_path: agents/Pickle
                        llm:
                          provider: google/gemini
                          model: gemini-3-flash-preview
                    """
                ).strip()
                + "\n",
                encoding="utf-8",
            )

            config = AppConfig.load(config_path)
            definition = config.resolve_agent_definition()

        self.assertEqual(config.default_agent, "Pickle")
        self.assertEqual(definition.agent_id, "Pickle")
        self.assertEqual(definition.workspace_path, root / "pickle_workspace")
        self.assertEqual(definition.behavior_path, root / "agents" / "Pickle")
        self.assertEqual(definition.behavior_instruction, "You are Pickle.")
        self.assertEqual(definition.model_config.provider, "google/gemini")
        self.assertEqual(definition.model_config.model, "gemini-3-flash-preview")
        self.assertEqual(definition.model_config.temperature, 1.0)
        self.assertEqual(definition.model_config.max_output_tokens, 1048576)
        self.assertEqual(definition.model_config.thinking_level, "minimal")
        self.assertEqual(
            definition.model_config.provider_options["thinking_level"], "minimal"
        )
