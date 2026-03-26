import unittest
from pathlib import Path
import sys

SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from myopenclaw.llm.config import ModelConfig
from myopenclaw.llm.factory import create_llm_provider


class ProviderFactoryTests(unittest.TestCase):
    def test_create_llm_provider_returns_gemini_provider(self) -> None:
        config = ModelConfig(
            provider="google/gemini",
            model="gemini-3-flash-preview",
            api_key="fake-key",
            provider_options={"thinking_level": "minimal"},
        )

        provider = create_llm_provider(config)

        self.assertEqual(provider.__class__.__name__, "GeminiProvider")
