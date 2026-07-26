"""extensions 段：settings 与 auth.json 深合并，auth 优先。"""

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from pickel.config.loader import Config


class ExtensionsConfigTests(unittest.TestCase):
    def test_settings_and_auth_sections_merge_with_auth_winning(self) -> None:
        with TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            (home / ".pickel").mkdir(parents=True)
            settings = {
                "default_agent": "Pickle",
                "default_llm": {"provider": "google/gemini", "model": "gemini-3-flash-preview"},
                "agents": {
                    "Pickle": {
                        "workspace_path": ".",
                        "behavior_path": ".",
                        "llm": {"provider": "google/gemini", "model": "gemini-3-flash-preview"},
                    }
                },
                "extensions": {
                    "openviking": {"enabled": True, "base_url": "https://ov.example", "user_key": "from-settings"}
                },
            }
            auth = {
                "extensions": {
                    "openviking": {"user_key": "from-auth", "account_id": "acct-1"}
                }
            }
            (home / ".pickel" / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
            (home / ".pickel" / "auth.json").write_text(json.dumps(auth), encoding="utf-8")

            config = Config.load(cwd=Path(tmp), home=home / ".pickel")

            section = config.extensions["openviking"]
            self.assertEqual("from-auth", section["user_key"])      # auth 覆盖 settings
            self.assertEqual("acct-1", section["account_id"])        # auth 独有的键保留
            self.assertEqual("https://ov.example", section["base_url"])  # settings 独有的键保留
            self.assertTrue(section["enabled"])

    def test_extensions_defaults_to_empty_dict(self) -> None:
        with TemporaryDirectory() as tmp:
            home = Path(tmp) / "home" / ".pickel"
            home.mkdir(parents=True)
            settings = {
                "default_agent": "Pickle",
                "default_llm": {"provider": "google/gemini", "model": "gemini-3-flash-preview"},
                "agents": {
                    "Pickle": {
                        "workspace_path": ".",
                        "behavior_path": ".",
                        "llm": {"provider": "google/gemini", "model": "gemini-3-flash-preview"},
                    }
                },
            }
            (home / "settings.json").write_text(json.dumps(settings), encoding="utf-8")

            config = Config.load(cwd=Path(tmp), home=home)

            self.assertEqual({}, config.extensions)


if __name__ == "__main__":
    unittest.main()
