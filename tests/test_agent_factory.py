import tempfile
import textwrap
import unittest
from pathlib import Path
import sys
from unittest.mock import patch

SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from myopenclaw.app.bootstrap import AppBootstrap
from myopenclaw.llm.config import ModelConfig
from myopenclaw.llm.provider import BaseLLMProvider, ChatRequest, ChatResult


class FakeProvider(BaseLLMProvider):
    @classmethod
    def from_config(cls, config: ModelConfig) -> "FakeProvider":
        return cls()

    async def chat(self, request: ChatRequest) -> ChatResult:
        return ChatResult(text="reply")


class AppBootstrapTests(unittest.TestCase):
    def test_from_config_path_returns_loaded_runtime_with_agent_definition(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
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
                            api_key: workspace-key
                            temperature: 1.0
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
            agent_dir = root / "agents" / "Pickle"
            agent_dir.mkdir(parents=True)
            (agent_dir / "AGENT.md").write_text(
                textwrap.dedent(
                    """
                    You are Pickle.
                    """
                ).strip()
                + "\n",
                encoding="utf-8",
            )

            with patch("myopenclaw.agent.agent.create_llm_provider", return_value=FakeProvider()):
                loaded_runtime = AppBootstrap.from_config_path(config_path=config_path)

        self.assertEqual(loaded_runtime.agent_id, "Pickle")
        self.assertEqual(
            loaded_runtime.agent.definition.model_config.api_key,
            "workspace-key",
        )
        self.assertEqual(
            loaded_runtime.agent.definition.workspace_path,
            root / "pickle_workspace",
        )
