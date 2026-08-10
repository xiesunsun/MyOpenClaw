import os
import textwrap
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from pickel.agents.agent import Agent
from pickel.app.boot import Boot
from pickel.extensions_host.loader import load_extensions
from pickel.tools.bus import ToolBus
from pickel.config.app_config import AppConfig
from pickel.conversations.service import SessionService
from pickel.runs.legacy_model_context_builder import LegacyModelContextBuilder
from pickel.conversations.session_sync import CompositeSessionSync, NoopSessionSync
from pickel.extensions.openviking.session_sync import OpenVikingSessionSync
from pickel.persistence.sqlite_session_repository import SQLiteSessionRepository
from tests.helpers.yaml_app_config import app_config_from_yaml_file


class BootTests(unittest.TestCase):
    def test_resolve_agent_loads_behavior_and_declared_defaults(self) -> None:
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "agents" / "Pickle").mkdir(parents=True)
            (root / "agents" / "Pickle" / "AGENT.md").write_text("You are Pickle.\n")
            (root / ".agent" / "skills" / "excel").mkdir(parents=True)
            (root / ".agent" / "skills" / "excel" / "SKILL.md").write_text(
                textwrap.dedent("""\
                    ---
                    name: excel
                    description: Analyze spreadsheets.
                    ---

                    # Excel
                    """),
                encoding="utf-8",
            )
            (root / "workspace").mkdir()
            config_path = root / "config.yaml"
            config_path.write_text(textwrap.dedent("""
                    default_agent: Pickle
                    default_file_access_mode: full
                    default_skills_path: .agent/skills
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
            agent = Boot(config).resolve_agent()

            self.assertIsInstance(agent, Agent)
            self.assertEqual("Pickle", agent.agent_id)
            self.assertEqual("You are Pickle.", agent.behavior_instruction)
            self.assertEqual(["echo"], agent.tool_ids)
            self.assertEqual(1, len(agent.skills))
            self.assertEqual(root / ".agent" / "skills", agent.skills_path)
            self.assertIn("Available skills:", agent.system_instruction)
            self.assertIn("excel: Analyze spreadsheets.", agent.system_instruction)

    def test_resolve_agent_rejects_skills_outside_workspace_without_full_access(
        self,
    ) -> None:
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "agents" / "Pickle").mkdir(parents=True)
            (root / "agents" / "Pickle" / "AGENT.md").write_text("You are Pickle.\n")
            (root / ".agent" / "skills" / "excel").mkdir(parents=True)
            (root / ".agent" / "skills" / "excel" / "SKILL.md").write_text(
                textwrap.dedent("""\
                    ---
                    name: excel
                    description: Analyze spreadsheets.
                    ---
                    """),
                encoding="utf-8",
            )
            (root / "workspace").mkdir()
            config_path = root / "config.yaml"
            config_path.write_text(textwrap.dedent("""
                    default_agent: Pickle
                    default_file_access_mode: workspace
                    default_skills_path: .agent/skills
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
                    """).strip())

            config = app_config_from_yaml_file(config_path)

            with self.assertRaisesRegex(ValueError, "requires file_access_mode: full"):
                Boot(config).resolve_agent()

    def test_build_run_injects_context_cli_turn_window(self) -> None:
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "agents" / "Pickle").mkdir(parents=True)
            (root / "agents" / "Pickle" / "AGENT.md").write_text("You are Pickle.\n")
            (root / "workspace").mkdir()
            config_path = root / "config.yaml"
            config_path.write_text(textwrap.dedent("""
                    default_agent: Pickle
                    react_max_steps: 16
                    context_cli_turn_window: 7
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
                    """).strip())

            _, run = Boot.from_config(
                app_config_from_yaml_file(config_path)
            ).build_run()

            self.assertEqual(16, run.strategy.max_steps)
            self.assertIsNotNone(run)
            self.assertIsInstance(
                run.model_context_builder,
                LegacyModelContextBuilder,
            )
            self.assertEqual(7, run.unit_window)

    def test_build_run_wires_openviking_session_recall_when_enabled(self) -> None:
        """OV session recall 经 Run.recall_sources 注入。"""
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
                        remote_agent_id: remote-pickle
                    extensions:
                      openviking:
                        enabled: true
                        base_url: https://openviking.example
                        account_id: pickel
                        user_id: ssunxie
                        user_key: secret
                        session_recall:
                          max_chars: 1234
                    """).strip())

            app_config = app_config_from_yaml_file(config_path)
            loaded = load_extensions(
                tool_bus=ToolBus(), app_config=app_config, home=root
            )
            _, run = Boot.from_config(app_config, extensions=loaded.registry).build_run(
                agent_id="Pickle"
            )

            self.assertIsNotNone(run)
            self.assertIsInstance(
                run.model_context_builder,
                LegacyModelContextBuilder,
            )
            self.assertFalse(hasattr(run, "session_recall_provider"))
            self.assertEqual(1, len(run.recall_sources))
            self.assertEqual(1234, run.recall_sources[0]._max_chars)

    def test_build_run_empty_recall_sources_when_openviking_disabled(self) -> None:
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
                    extensions:
                      openviking:
                        enabled: false
                        base_url: https://openviking.example
                        account_id: pickel
                        user_id: ssunxie
                        user_key: secret
                    """).strip())

            app_config = app_config_from_yaml_file(config_path)
            loaded = load_extensions(
                tool_bus=ToolBus(), app_config=app_config, home=root
            )
            _, run = Boot.from_config(
                app_config, extensions=loaded.registry
            ).build_run()
            self.assertEqual([], run.recall_sources)

    def test_build_session_service_returns_session_service_with_sqlite_repo(
        self,
    ) -> None:
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
                    """).strip())

            pickel_home = root / "pickel-home"
            pickel_home.mkdir()
            with patch.dict(os.environ, {"PICKEL_HOME": str(pickel_home)}):
                service = Boot.from_config(
                    app_config_from_yaml_file(config_path)
                ).build_session_service()

            self.assertIsInstance(service, SessionService)
            self.assertIsInstance(service._repository, SQLiteSessionRepository)
            self.assertEqual(
                pickel_home / "sessions.db",
                service._repository.db_path,
            )
            self.assertIsInstance(service._session_sync, CompositeSessionSync)
            self.assertEqual([], service._session_sync._syncs)

    def test_build_session_service_wires_openviking_sync_when_enabled(self) -> None:
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
                        remote_agent_id: remote-pickle
                    extensions:
                      openviking:
                        enabled: true
                        base_url: https://openviking.example
                        account_id: pickel
                        user_id: ssunxie
                        user_key: secret
                        commit_after_minutes: 15
                        commit_after_turns: 4
                        tool_output_max_chars: 1000
                    """).strip())

            app_config = app_config_from_yaml_file(config_path)
            loaded = load_extensions(
                tool_bus=ToolBus(), app_config=app_config, home=root
            )
            service = Boot.from_config(
                app_config, extensions=loaded.registry
            ).build_session_service(agent_id="Pickle")

            self.assertIsInstance(service._session_sync, CompositeSessionSync)
            self.assertEqual(1, len(service._session_sync._syncs))
            self.assertIsInstance(
                service._session_sync._syncs[0], OpenVikingSessionSync
            )


if __name__ == "__main__":
    unittest.main()
