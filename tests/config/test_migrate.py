"""config migrate：旧 yaml → 分层文件；仅用临时目录，不碰真实 home。"""

from __future__ import annotations

import json
import os
import sqlite3
import textwrap
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import yaml
from typer.testing import CliRunner

from pickel.cli.main import app
from pickel.config.migrate import migrate_from_yaml


class MigrateFromYamlTests(unittest.TestCase):

    def test_legacy_openviking_section_migrates_under_extensions(self) -> None:
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            home = root / "home"
            project = root / "project"
            project.mkdir()
            (project / "agents" / "Pickle").mkdir(parents=True)
            (project / "agents" / "Pickle" / "AGENT.md").write_text(
                "# Pickle\n", encoding="utf-8"
            )
            config_path = project / "config.yaml"
            raw = yaml.safe_load(textwrap.dedent(self._minimal_yaml()))
            raw["openviking"]["enabled"] = True
            raw["openviking"]["session_recall"] = {
                "enabled": True,
                "max_chars": 1234,
            }
            config_path.write_text(yaml.safe_dump(raw), encoding="utf-8")

            migrate_from_yaml(config_path, home=home, project_root=project)

            settings = json.loads((home / "settings.json").read_text(encoding="utf-8"))
            auth = json.loads((home / "auth.json").read_text(encoding="utf-8"))

            strategy = settings["extensions"]["openviking"]
            self.assertTrue(strategy["enabled"])
            self.assertEqual(30, strategy["commit_after_minutes"])
            self.assertEqual(8, strategy["commit_after_turns"])
            self.assertEqual(
                {"enabled": True, "max_chars": 1234}, strategy["session_recall"]
            )
            self.assertNotIn("user_key", strategy)
            self.assertNotIn("openviking", settings)

            secrets = auth["extensions"]["openviking"]
            self.assertEqual("${OPENVIKING_USER_KEY}", secrets["user_key"])
            self.assertEqual("${OPENVIKING_BASE_URL}", secrets["base_url"])
            self.assertNotIn("commit_after_minutes", secrets)
            self.assertNotIn("openviking", auth)

    def _write_yaml(self, path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(content).strip() + "\n", encoding="utf-8")

    def _minimal_yaml(self) -> str:
        return """
            default_agent: Pickle
            react_max_steps: 42
            context_cli_turn_window: 3
            default_file_access_mode: full
            default_skills_path: .agent/skills
            default_llm:
              provider: anthropic
              model: claude-test
            providers:
              anthropic:
                models:
                  claude-test:
                    api_key: ${ANTHROPIC_API_KEY_TEST}
                    api_base: https://api.anthropic.com
                    temperature: 0.5
                    max_output_tokens: 1024
                    provider_options: {}
              google/gemini:
                models:
                  gemini-flash:
                    temperature: 1.0
                    max_output_tokens: 2048
                    provider_options:
                      thinking: low
            agents:
              Pickle:
                workspace_path: pickle_workspace
                behavior_path: agents/Pickle
                file_access_mode: full
                remote_agent_id: ${OPENVIKING_AGENT_ID}
                llm:
                  provider: anthropic
                  model: claude-test
                tools:
                  - read_file
                  - shell_exec
            openviking:
              enabled: false
              base_url: ${OPENVIKING_BASE_URL}
              account_id: ${OPENVIKING_ACCOUNT_ID}
              user_id: ${OPENVIKING_USER_ID}
              user_key: ${OPENVIKING_USER_KEY}
              timeout_seconds: 30
              commit_after_minutes: 30
              commit_after_turns: 8
              tool_output_max_chars: 4000
              session_recall:
                enabled: false
                max_chars: 6000
                limit: 5
            """

    def test_migrate_writes_settings_models_auth_and_agents(self) -> None:
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            home = root / "home"
            project = root / "project"
            project.mkdir()
            (project / "agents" / "Pickle").mkdir(parents=True)
            (project / "agents" / "Pickle" / "AGENT.md").write_text(
                "# Pickle\n", encoding="utf-8"
            )
            config_path = project / "config.yaml"
            self._write_yaml(config_path, self._minimal_yaml())

            with patch.dict(os.environ, {"PICKEL_HOME": str(home)}):
                summary = migrate_from_yaml(
                    config_path, home=home, project_root=project
                )

            self.assertEqual(str(home), summary["home"])
            self.assertTrue((home / "settings.json").is_file())
            self.assertTrue((home / "models.json").is_file())
            self.assertTrue((home / "auth.json").is_file())

            settings = json.loads((home / "settings.json").read_text(encoding="utf-8"))
            self.assertEqual("Pickle", settings["default_agent"])
            self.assertEqual(42, settings["react_max_steps"])
            self.assertEqual(3, settings["context_cli_turn_window"])
            self.assertEqual("full", settings["default_file_access_mode"])
            self.assertEqual(".agent/skills", settings["default_skills_path"])
            self.assertEqual(
                {"provider": "anthropic", "model": "claude-test"},
                settings["default_llm"],
            )
            # openviking 策略无密钥，折算进 extensions 段
            self.assertNotIn("openviking", settings)
            ov_strategy = settings["extensions"]["openviking"]
            self.assertNotIn("base_url", ov_strategy)
            self.assertNotIn("user_key", ov_strategy)
            self.assertFalse(ov_strategy["enabled"])
            self.assertEqual(30, ov_strategy["timeout_seconds"])

            models = json.loads((home / "models.json").read_text(encoding="utf-8"))
            anthropic_model = models["providers"]["anthropic"]["models"]["claude-test"]
            self.assertNotIn("api_key", anthropic_model)
            self.assertNotIn("api_base", anthropic_model)
            self.assertEqual(0.5, anthropic_model["temperature"])

            auth = json.loads((home / "auth.json").read_text(encoding="utf-8"))
            self.assertEqual(
                "${ANTHROPIC_API_KEY_TEST}",
                auth["providers"]["anthropic"]["api_key"],
            )
            self.assertEqual(
                "https://api.anthropic.com",
                auth["providers"]["anthropic"]["api_base"],
            )
            self.assertEqual(
                "${OPENVIKING_BASE_URL}", auth["extensions"]["openviking"]["base_url"]
            )
            self.assertEqual(
                "${OPENVIKING_USER_KEY}", auth["extensions"]["openviking"]["user_key"]
            )

            agent_yaml = project / "agents" / "Pickle" / "agent.yaml"
            self.assertTrue(agent_yaml.is_file())
            # 不覆盖 AGENT.md
            self.assertEqual(
                "# Pickle\n",
                (project / "agents" / "Pickle" / "AGENT.md").read_text(
                    encoding="utf-8"
                ),
            )
            agent_text = agent_yaml.read_text(encoding="utf-8")
            self.assertIn("workspace_path: pickle_workspace", agent_text)
            self.assertIn("${OPENVIKING_AGENT_ID}", agent_text)
            # 默认 behavior 省略
            self.assertNotIn("behavior_path", agent_text)

            self.assertTrue((project / "config.yaml.bak").is_file())
            self.assertTrue(config_path.is_file())

    def test_auth_merge_does_not_overwrite_existing_secrets(self) -> None:
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            home = root / "home"
            home.mkdir()
            project = root / "project"
            project.mkdir()
            (home / "auth.json").write_text(
                json.dumps(
                    {
                        "providers": {
                            "anthropic": {
                                "api_key": "keep-me",
                                "api_base": "https://keep.example",
                            }
                        },
                        "extensions": {"openviking": {"user_key": "keep-ov"}},
                    }
                ),
                encoding="utf-8",
            )
            config_path = project / "config.yaml"
            self._write_yaml(config_path, self._minimal_yaml())

            summary = migrate_from_yaml(
                config_path, home=home, project_root=project
            )

            auth = json.loads((home / "auth.json").read_text(encoding="utf-8"))
            self.assertEqual("keep-me", auth["providers"]["anthropic"]["api_key"])
            self.assertEqual(
                "https://keep.example",
                auth["providers"]["anthropic"]["api_base"],
            )
            self.assertEqual(
                "keep-ov", auth["extensions"]["openviking"]["user_key"]
            )
            # 未冲突的 openviking 键可补入
            self.assertEqual(
                "${OPENVIKING_BASE_URL}",
                auth["extensions"]["openviking"]["base_url"],
            )
            self.assertTrue(summary["auth_merged"])
            self.assertIn("providers.anthropic.api_key", summary["auth_skipped_keys"])

    def test_default_skills_path_prefers_relative_under_project(self) -> None:
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            home = root / "home"
            project = root / "project"
            skills = project / ".agent" / "skills"
            skills.mkdir(parents=True)
            config_path = project / "config.yaml"
            yaml_text = self._minimal_yaml().replace(
                "default_skills_path: .agent/skills",
                f"default_skills_path: {skills}",
            )
            self._write_yaml(config_path, yaml_text)

            migrate_from_yaml(config_path, home=home, project_root=project)

            settings = json.loads((home / "settings.json").read_text(encoding="utf-8"))
            self.assertEqual(".agent/skills", settings["default_skills_path"])

    def test_sessions_copy_when_global_empty(self) -> None:
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            home = root / "home"
            project = root / "project"
            legacy_dir = project / ".pickel"
            legacy_dir.mkdir(parents=True)
            legacy_db = legacy_dir / "sessions.db"
            with sqlite3.connect(legacy_db) as conn:
                conn.executescript(
                    """
                    PRAGMA user_version = 3;
                    CREATE TABLE sessions (
                        session_id TEXT PRIMARY KEY,
                        agent_id TEXT NOT NULL,
                        cwd TEXT,
                        leaf_id TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        status TEXT NOT NULL,
                        title TEXT
                    );
                    INSERT INTO sessions VALUES (
                        's1', 'Pickle', NULL, NULL,
                        '2026-01-01T00:00:00+00:00',
                        '2026-01-01T00:00:00+00:00',
                        'active', NULL
                    );
                    """
                )
            config_path = project / "config.yaml"
            self._write_yaml(config_path, self._minimal_yaml())

            summary = migrate_from_yaml(
                config_path, home=home, project_root=project
            )

            global_db = home / "sessions.db"
            self.assertTrue(global_db.is_file())
            self.assertEqual("copied", summary["sessions"]["action"])
            self.assertFalse(legacy_db.exists())
            self.assertTrue(Path(str(summary["sessions"]["legacy_bak"])).is_file())

            with sqlite3.connect(global_db) as conn:
                row = conn.execute(
                    "SELECT cwd FROM sessions WHERE session_id = 's1'"
                ).fetchone()
            self.assertEqual(str(project.resolve()), row[0])

    def test_cli_config_migrate(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            home = root / "home"
            project = root / "project"
            project.mkdir()
            config_path = project / "config.yaml"
            self._write_yaml(config_path, self._minimal_yaml())

            with patch.dict(os.environ, {"PICKEL_HOME": str(home)}):
                result = runner.invoke(
                    app,
                    ["config", "migrate", "--from", str(config_path)],
                )

            self.assertEqual(0, result.exit_code, result.output)
            self.assertIn("settings:", result.stdout)
            self.assertTrue((home / "settings.json").is_file())


if __name__ == "__main__":
    unittest.main()
