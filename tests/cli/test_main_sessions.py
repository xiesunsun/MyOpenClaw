import os
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, Mock, patch

from typer.testing import CliRunner

from pickel.cli.main import app
from pickel.conversations.service import SessionNotFoundError, SessionService
from pickel.conversations.session import Session
from pickel.conversations.session_preview import SessionPreview
from pickel.integrations.openviking.session_sync import NoopSessionSync
from pickel.persistence.sqlite_session_repository import SQLiteSessionRepository


class MainSessionsCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runner = CliRunner()

    def test_sessions_command_lists_previews(self) -> None:
        fake_service = Mock()
        fake_service.list_sessions.return_value = [
            SessionPreview(
                session_id="session-1",
                agent_id="Pickle",
                created_at=datetime(2026, 4, 13, tzinfo=timezone.utc),
                updated_at=datetime(2026, 4, 13, 1, tzinfo=timezone.utc),
                status="active",
                message_count=2,
                last_message="hello",
            )
        ]
        fake_boot = Mock()
        fake_boot.build_session_service.return_value = fake_service

        with patch("pickel.cli.main._boot", return_value=fake_boot):
            result = self.runner.invoke(app, ["sessions"])

        self.assertEqual(0, result.exit_code)
        self.assertIn("session-1", result.stdout)
        self.assertIn("hello", result.stdout)
        fake_service.list_sessions.assert_called_once_with(all_sessions=False)

    def test_sessions_command_all_flag_passes_all_sessions_true(self) -> None:
        fake_service = Mock()
        fake_service.list_sessions.return_value = []
        fake_boot = Mock()
        fake_boot.build_session_service.return_value = fake_service

        with patch("pickel.cli.main._boot", return_value=fake_boot):
            result = self.runner.invoke(app, ["sessions", "--all"])

        self.assertEqual(0, result.exit_code)
        fake_service.list_sessions.assert_called_once_with(all_sessions=True)

    def test_sessions_command_shows_full_session_id_without_ellipsis(self) -> None:
        fake_service = Mock()
        session_id = "910d3d42-4948-4bd8-9031-1234567890abcdef"
        fake_service.list_sessions.return_value = [
            SessionPreview(
                session_id=session_id,
                agent_id="Pickle",
                created_at=datetime(2026, 4, 13, tzinfo=timezone.utc),
                updated_at=datetime(2026, 4, 13, 1, tzinfo=timezone.utc),
                status="active",
                message_count=2,
                last_message="hello " * 20,
            )
        ]
        fake_boot = Mock()
        fake_boot.build_session_service.return_value = fake_service

        with patch("pickel.cli.main._boot", return_value=fake_boot):
            result = self.runner.invoke(
                app,
                ["sessions"],
                env={"COLUMNS": "120"},
            )

        self.assertEqual(0, result.exit_code)
        self.assertIn(session_id, result.stdout)
        self.assertNotIn("910d3d42-4948-4bd8-9031-…", result.stdout)

    def test_sessions_cli_cwd_filter_and_all_with_pickel_home(self) -> None:
        """两个 cwd 各建 session；默认只见当前 cwd；--all 全见。"""
        with TemporaryDirectory() as tmpdir:
            home = Path(tmpdir) / "pickel-home"
            home.mkdir()
            proj_a = Path(tmpdir) / "proj-a"
            proj_b = Path(tmpdir) / "proj-b"
            proj_a.mkdir()
            proj_b.mkdir()

            env = {**os.environ, "PICKEL_HOME": str(home)}
            with patch.dict(os.environ, env, clear=True):
                db_path = home / "sessions.db"
                service = SessionService(
                    SQLiteSessionRepository(db_path),
                    NoopSessionSync(),
                )
                sa = service.start(agent_id="Pickle", cwd=str(proj_a.resolve()))
                sb = service.start(agent_id="Pickle", cwd=str(proj_b.resolve()))

                fake_boot = Mock()
                fake_boot.build_session_service.return_value = service

                with patch(
                    "pickel.conversations.service.Path.cwd",
                    return_value=proj_a.resolve(),
                ), patch("pickel.cli.main._boot", return_value=fake_boot):
                    default_result = self.runner.invoke(
                        app,
                        ["sessions"],
                        env={**env, "COLUMNS": "120"},
                    )
                with patch("pickel.cli.main._boot", return_value=fake_boot):
                    all_result = self.runner.invoke(
                        app,
                        ["sessions", "--all"],
                        env={**env, "COLUMNS": "120"},
                    )

            self.assertEqual(0, default_result.exit_code, default_result.stdout)
            self.assertIn(sa.session_id, default_result.stdout)
            self.assertNotIn(sb.session_id, default_result.stdout)

            self.assertEqual(0, all_result.exit_code, all_result.stdout)
            self.assertIn(sa.session_id, all_result.stdout)
            self.assertIn(sb.session_id, all_result.stdout)

    def test_session_id_option_resumes_existing_session(self) -> None:
        fake_loop = Mock()
        fake_loop.run = AsyncMock(return_value=None)
        fake_boot = Mock()

        with (
            patch("pickel.cli.main._boot", return_value=fake_boot),
            patch(
                "pickel.cli.main.ChatLoop.from_boot", return_value=fake_loop
            ) as from_boot,
        ):
            result = self.runner.invoke(
                app,
                ["--session-id", "session-1"],
            )

        self.assertEqual(0, result.exit_code)
        from_boot.assert_called_once_with(
            boot=fake_boot,
            agent_id=None,
            session_id="session-1",
        )

    def test_cli_loads_layered_config_without_config_flag(self) -> None:
        """唯一路径：Config.load + Boot.from_config。"""
        import json

        with TemporaryDirectory() as tmpdir:
            home = Path(tmpdir) / "pickel-home"
            project = Path(tmpdir) / "project"
            (project / ".pickel").mkdir(parents=True)
            (project / "agents" / "Pickle").mkdir(parents=True)
            (project / "agents" / "Pickle" / "AGENT.md").write_text(
                "You are Pickle.\n", encoding="utf-8"
            )
            (project / "agents" / "Pickle" / "agent.yaml").write_text(
                "\n".join(
                    [
                        "workspace_path: workspace",
                        "tools: []",
                    ]
                ),
                encoding="utf-8",
            )
            (project / "workspace").mkdir()

            settings = {
                "default_agent": "Pickle",
                "default_llm": {
                    "provider": "google/gemini",
                    "model": "gemini-3-flash-preview",
                },
            }
            models = {
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
            }
            auth = {
                "providers": {
                    "google/gemini": {
                        "api_key": "test-key",
                        "api_base": "https://example.com/v1",
                    }
                }
            }
            home.mkdir()
            (home / "settings.json").write_text(
                json.dumps(settings), encoding="utf-8"
            )
            (home / "models.json").write_text(json.dumps(models), encoding="utf-8")
            (home / "auth.json").write_text(json.dumps(auth), encoding="utf-8")

            fake_service = Mock()
            fake_service.list_sessions.return_value = []
            fake_boot = Mock()
            fake_boot.build_session_service.return_value = fake_service

            env = {**os.environ, "PICKEL_HOME": str(home)}
            with (
                patch.dict(os.environ, env, clear=True),
                patch(
                    "pickel.cli.main.Path.cwd", return_value=project.resolve()
                ),
                patch(
                    "pickel.config.loader.Path.cwd",
                    return_value=project.resolve(),
                ),
                patch(
                    "pickel.cli.main.Boot.from_config", return_value=fake_boot
                ) as from_config,
            ):
                result = self.runner.invoke(
                    app,
                    ["sessions"],
                    env={**env, "COLUMNS": "120"},
                )

            self.assertEqual(0, result.exit_code, result.stdout + result.stderr)
            from_config.assert_called_once()
            app_config = from_config.call_args.args[0]
            self.assertEqual("Pickle", app_config.default_agent)
            fake_service.list_sessions.assert_called_once_with(all_sessions=False)

    def test_sessions_delete_deletes_remote_then_local_for_session_agent(self) -> None:
        lookup_service = Mock()
        lookup_service.resume.return_value = Session(
            session_id="session-1",
            agent_id="Pickle",
        )
        delete_service = Mock()
        fake_boot = Mock()
        fake_boot.build_session_service.side_effect = [
            lookup_service,
            delete_service,
        ]

        with patch("pickel.cli.main._boot", return_value=fake_boot):
            result = self.runner.invoke(
                app,
                ["sessions", "delete", "session-1"],
            )

        self.assertEqual(0, result.exit_code)
        self.assertIn("Deleted session session-1", result.stdout)
        fake_boot.build_session_service.assert_any_call()
        fake_boot.build_session_service.assert_any_call(agent_id="Pickle")
        delete_service.delete.assert_called_once_with(session_id="session-1")

    def test_sessions_delete_reports_missing_session(self) -> None:
        lookup_service = Mock()
        lookup_service.resume.side_effect = SessionNotFoundError(
            "Session not found: missing"
        )
        fake_boot = Mock()
        fake_boot.build_session_service.return_value = lookup_service

        with patch("pickel.cli.main._boot", return_value=fake_boot):
            result = self.runner.invoke(
                app,
                ["sessions", "delete", "missing"],
            )

        self.assertNotEqual(0, result.exit_code)
        self.assertIn("Session not found: missing", result.stderr)


if __name__ == "__main__":
    unittest.main()
