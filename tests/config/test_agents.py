"""Agents 目录扫描：agents/<id>/AGENT.md + agent.yaml。"""

from __future__ import annotations

import json
import os
import textwrap
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from pickel.config.agents import load_agent_dir, scan_agents
from pickel.config.loader import Config


class AgentsScanTests(unittest.TestCase):
    def _write_json(self, path: Path, data: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")

    def _minimal_home(self, home: Path) -> None:
        self._write_json(
            home / "settings.json",
            {
                "default_agent": "Foo",
                "default_llm": {
                    "provider": "google/gemini",
                    "model": "gemini-3-flash-preview",
                },
            },
        )
        self._write_json(
            home / "models.json",
            {
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
            },
        )
        self._write_json(home / "auth.json", {"providers": {}})

    def test_scan_agents_registers_dir_with_agent_md_and_yaml(self) -> None:
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            foo = root / "agents" / "Foo"
            foo.mkdir(parents=True)
            (foo / "AGENT.md").write_text("# Foo\n", encoding="utf-8")
            (foo / "agent.yaml").write_text(
                textwrap.dedent("""
                    workspace_path: foo_ws
                    tools:
                      - read_file
                    extensions:
                      - mcp
                    file_access_mode: full
                    llm:
                      provider: google/gemini
                      model: gemini-3-flash-preview
                    """).strip(),
                encoding="utf-8",
            )
            (root / "foo_ws").mkdir()

            scanned = scan_agents(root)

            self.assertIn("Foo", scanned)
            self.assertEqual("foo_ws", scanned["Foo"]["workspace_path"])
            self.assertEqual(["read_file"], scanned["Foo"]["tools"])
            self.assertEqual(["mcp"], scanned["Foo"]["extensions"])
            self.assertEqual("agents/Foo", scanned["Foo"]["behavior_path"])

    def test_config_load_finds_directory_agent(self) -> None:
        with TemporaryDirectory() as tmpdir:
            home = Path(tmpdir) / "home"
            project = Path(tmpdir) / "project"
            (project / ".pickel").mkdir(parents=True)
            foo = project / "agents" / "Foo"
            foo.mkdir(parents=True)
            (foo / "AGENT.md").write_text("# Foo\n", encoding="utf-8")
            (foo / "agent.yaml").write_text(
                textwrap.dedent("""
                    workspace_path: foo_ws
                    tools:
                      - read_file
                      - write_file
                    file_access_mode: full
                    llm:
                      provider: google/gemini
                      model: gemini-3-flash-preview
                    """).strip(),
                encoding="utf-8",
            )
            (project / "foo_ws").mkdir()
            self._minimal_home(home)

            config = Config.load(cwd=project, home=home)
            agent = config.get_agent_config("Foo")

            self.assertEqual(project.resolve() / "foo_ws", agent.workspace_path)
            self.assertEqual(project.resolve() / "agents" / "Foo", agent.behavior_path)
            self.assertEqual(["read_file", "write_file"], agent.tools)
            self.assertEqual("full", agent.file_access_mode.value)
            assert agent.llm is not None
            self.assertEqual("google/gemini", agent.llm.provider)
            self.assertEqual("gemini-3-flash-preview", agent.llm.model)

    def test_directory_agent_is_sole_agent_source(self) -> None:
        with TemporaryDirectory() as tmpdir:
            home = Path(tmpdir) / "home"
            project = Path(tmpdir) / "project"
            (project / ".pickel").mkdir(parents=True)
            foo = project / "agents" / "Foo"
            foo.mkdir(parents=True)
            (foo / "AGENT.md").write_text("# Foo\n", encoding="utf-8")
            (foo / "agent.yaml").write_text(
                textwrap.dedent("""
                    workspace_path: from_dir
                    tools:
                      - from_dir_tool
                    """).strip(),
                encoding="utf-8",
            )
            (project / "from_dir").mkdir()
            self._minimal_home(home)

            config = Config.load(cwd=project, home=home)
            agent = config.get_agent_config("Foo")

            self.assertEqual(project.resolve() / "from_dir", agent.workspace_path)
            self.assertEqual(["from_dir_tool"], agent.tools)

    def test_expand_env_in_agent_yaml(self) -> None:
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            foo = root / "agents" / "Foo"
            foo.mkdir(parents=True)
            (foo / "AGENT.md").write_text("# Foo\n", encoding="utf-8")
            (foo / "agent.yaml").write_text(
                "workspace_path: ws\nremote_agent_id: ${TEST_AGENT_REMOTE_ID}\n",
                encoding="utf-8",
            )

            with patch.dict(
                os.environ, {"TEST_AGENT_REMOTE_ID": "remote-123"}, clear=False
            ):
                data = load_agent_dir(foo, root)

            self.assertEqual("remote-123", data["remote_agent_id"])

    def test_register_with_agent_yaml_only(self) -> None:
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            bar = root / "agents" / "Bar"
            bar.mkdir(parents=True)
            (bar / "agent.yaml").write_text(
                "workspace_path: bar_ws\ntools: []\n",
                encoding="utf-8",
            )

            scanned = scan_agents(root)

            self.assertIn("Bar", scanned)
            self.assertEqual("agents/Bar", scanned["Bar"]["behavior_path"])

    def test_skip_empty_subdir(self) -> None:
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            empty = root / "agents" / "Empty"
            empty.mkdir(parents=True)

            self.assertEqual({}, scan_agents(root))


if __name__ == "__main__":
    unittest.main()
