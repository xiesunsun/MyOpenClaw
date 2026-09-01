"""settings.json 写回：save / update / set_default_llm。"""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from pickel.config.settings import (
    load_settings,
    save_settings,
    set_default_llm,
    settings_path,
    update_settings,
)
from pickel.shared.model_config import ModelSelection


class SettingsWriteTests(unittest.TestCase):
    def test_save_and_reload(self) -> None:
        with TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "settings.json"
            data = {
                "default_agent": "Pickle",
                "default_llm": {
                    "provider": "anthropic",
                    "model": "claude-sonnet-4-6",
                },
            }
            save_settings(path, data)

            self.assertTrue(path.is_file())
            reloaded = load_settings(path)
            self.assertEqual(data, reloaded)
            raw = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data, raw)

    def test_update_deep_merges_and_preserves_other_keys(self) -> None:
        with TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "settings.json"
            save_settings(
                path,
                {
                    "default_agent": "Pickle",
                    "react_max_steps": 8,
                    "openviking": {"enabled": False, "timeout_seconds": 30},
                    "default_llm": {
                        "provider": "anthropic",
                        "model": "old-model",
                    },
                },
            )

            merged = update_settings(
                path,
                {
                    "default_llm": {
                        "provider": "google/gemini",
                        "model": "gemini-3-flash-preview",
                    },
                    "openviking": {"enabled": True},
                },
            )

            self.assertEqual("Pickle", merged["default_agent"])
            self.assertEqual(8, merged["react_max_steps"])
            self.assertEqual(
                {
                    "provider": "google/gemini",
                    "model": "gemini-3-flash-preview",
                },
                merged["default_llm"],
            )
            self.assertEqual(
                {"enabled": True, "timeout_seconds": 30},
                merged["openviking"],
            )

            reloaded = load_settings(path)
            self.assertEqual(merged, reloaded)

    def test_update_creates_file_when_missing(self) -> None:
        with TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "nested" / "settings.json"
            merged = update_settings(
                path,
                {"default_agent": "Pickle"},
            )
            self.assertEqual({"default_agent": "Pickle"}, merged)
            self.assertTrue(path.is_file())

    def test_set_default_llm_global(self) -> None:
        with TemporaryDirectory() as tmpdir:
            home = Path(tmpdir) / "home"
            home.mkdir()
            save_settings(
                settings_path(home),
                {
                    "default_agent": "Pickle",
                    "default_llm": {
                        "provider": "anthropic",
                        "model": "old",
                    },
                },
            )

            written = set_default_llm(
                ModelSelection(
                    provider="google/gemini", model="gemini-3-flash-preview"
                ),
                scope="global",
                home=home,
            )
            self.assertEqual(settings_path(home), written)

            data = load_settings(written)
            self.assertEqual("Pickle", data["default_agent"])
            self.assertEqual(
                {
                    "provider": "google/gemini",
                    "model": "gemini-3-flash-preview",
                },
                data["default_llm"],
            )

    def test_set_default_llm_project(self) -> None:
        with TemporaryDirectory() as tmpdir:
            project = Path(tmpdir) / "proj"
            (project / ".pickel").mkdir(parents=True)

            written = set_default_llm(
                ModelSelection(provider="anthropic", model="claude-opus-4-7"),
                scope="project",
                project_root=project,
            )
            expected = settings_path(project / ".pickel")
            self.assertEqual(expected, written)
            data = load_settings(written)
            self.assertEqual(
                {"provider": "anthropic", "model": "claude-opus-4-7"},
                data["default_llm"],
            )

    def test_set_default_llm_project_requires_root(self) -> None:
        with TemporaryDirectory() as tmpdir:
            cwd = Path(tmpdir) / "empty"
            cwd.mkdir()
            with patch(
                "pickel.config.settings.discover_project_root", return_value=None
            ):
                with self.assertRaises(ValueError):
                    set_default_llm(
                        ModelSelection(provider="anthropic", model="x"),
                        scope="project",
                    )

    def test_update_preserves_env_placeholders(self) -> None:
        """写回合并用原文，避免把 ${ENV} 展开进文件。"""
        with TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "settings.json"
            save_settings(
                path,
                {
                    "default_agent": "Pickle",
                    "note": "${MY_NOTE}",
                    "default_llm": {
                        "provider": "anthropic",
                        "model": "old",
                    },
                },
            )
            with patch.dict(os.environ, {"MY_NOTE": "expanded-value"}):
                update_settings(
                    path,
                    {
                        "default_llm": {
                            "provider": "anthropic",
                            "model": "new",
                        }
                    },
                )
            raw = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual("${MY_NOTE}", raw["note"])
            self.assertEqual("new", raw["default_llm"]["model"])
