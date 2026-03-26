import unittest
import tempfile
import textwrap
from pathlib import Path
import sys

SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from myopenclaw.config.app_config import AppConfig


class AgentDefinitionResolutionTests(unittest.TestCase):
    def test_resolve_agent_definition_falls_back_to_default_llm(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            pickle_dir = root / "agents" / "Pickle"
            hajimi_dir = root / "agents" / "hajimi"
            pickle_dir.mkdir(parents=True)
            hajimi_dir.mkdir(parents=True)
            (pickle_dir / "AGENT.md").write_text("You are Pickle.\n", encoding="utf-8")
            (hajimi_dir / "AGENT.md").write_text("You are hajimi.\n", encoding="utf-8")
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
                            provider_options: {}

                    agents:
                      Pickle:
                        workspace_path: pickle_workspace
                        behavior_path: agents/Pickle
                        llm:
                          provider: google/gemini
                          model: gemini-3-flash-preview
                      hajimi:
                        workspace_path: hajimi_workspace
                        behavior_path: agents/hajimi
                    """
                ).strip()
                + "\n",
                encoding="utf-8",
            )

            definition = AppConfig.load(config_path).resolve_agent_definition("hajimi")

        self.assertEqual(definition.agent_id, "hajimi")
        self.assertEqual(definition.model_config.provider, "google/gemini")
        self.assertEqual(definition.model_config.model, "gemini-3-flash-preview")
        self.assertEqual(definition.behavior_instruction, "You are hajimi.")
