import os
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, Mock, patch

from typer.testing import CliRunner

from myopenclaw.cli.main import app
from myopenclaw.conversations.service import SessionNotFoundError, SessionService
from myopenclaw.conversations.session import Session
from myopenclaw.conversations.session_preview import SessionPreview
from myopenclaw.integrations.openviking.session_sync import NoopSessionSync
from myopenclaw.persistence.sqlite_session_repository import SQLiteSessionRepository


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
        fake_assembly = Mock()
        fake_assembly.build_session_service.return_value = fake_service

        with patch("myopenclaw.cli.main.AppAssembly.from_config_path", return_value=fake_assembly):
            result = self.runner.invoke(app, ["sessions", "--config", "config.yaml"])

        self.assertEqual(0, result.exit_code)
        self.assertIn("session-1", result.stdout)
        self.assertIn("hello", result.stdout)
        fake_service.list_sessions.assert_called_once_with(all_sessions=False)

    def test_sessions_command_all_flag_passes_all_sessions_true(self) -> None:
        fake_service = Mock()
        fake_service.list_sessions.return_value = []
        fake_assembly = Mock()
        fake_assembly.build_session_service.return_value = fake_service

        with patch("myopenclaw.cli.main.AppAssembly.from_config_path", return_value=fake_assembly):
            result = self.runner.invoke(
                app, ["sessions", "--all", "--config", "config.yaml"]
            )

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
        fake_assembly = Mock()
        fake_assembly.build_session_service.return_value = fake_service

        with patch("myopenclaw.cli.main.AppAssembly.from_config_path", return_value=fake_assembly):
            result = self.runner.invoke(
                app,
                ["sessions", "--config", "config.yaml"],
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

            # 最小 config 供 from_config_path 加载
            config_path = proj_a / "config.yaml"
            config_path.write_text(
                "\n".join(
                    [
                        "default_agent: Pickle",
                        "default_llm:",
                        "  provider: google/gemini",
                        "  model: gemini-3-flash-preview",
                        "providers:",
                        "  google/gemini:",
                        "    models:",
                        "      gemini-3-flash-preview:",
                        "        temperature: 1.0",
                        "        max_output_tokens: 1024",
                        "        provider_options: {}",
                        "agents:",
                        "  Pickle:",
                        "    workspace_path: workspace",
                        "    behavior_path: agents/Pickle",
                    ]
                )
            )
            (proj_a / "agents" / "Pickle").mkdir(parents=True)
            (proj_a / "agents" / "Pickle" / "AGENT.md").write_text("You are Pickle.\n")
            (proj_a / "workspace").mkdir()

            env = {**os.environ, "PICKEL_HOME": str(home)}
            with patch.dict(os.environ, env, clear=True):
                db_path = home / "sessions.db"
                service = SessionService(
                    SQLiteSessionRepository(db_path),
                    NoopSessionSync(),
                )
                sa = service.start(agent_id="Pickle", cwd=str(proj_a.resolve()))
                sb = service.start(agent_id="Pickle", cwd=str(proj_b.resolve()))

                # 在 proj-a 下默认 list：只应出现 sa
                with patch(
                    "myopenclaw.conversations.service.Path.cwd",
                    return_value=proj_a.resolve(),
                ):
                    default_result = self.runner.invoke(
                        app,
                        ["sessions", "--config", str(config_path)],
                        env={**env, "COLUMNS": "120"},
                    )
                all_result = self.runner.invoke(
                    app,
                    ["sessions", "--all", "--config", str(config_path)],
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

        with patch("myopenclaw.cli.main.ChatLoop.from_config_path", return_value=fake_loop) as from_config_path:
            result = self.runner.invoke(
                app,
                ["--config", "config.yaml", "--session-id", "session-1"],
            )

        self.assertEqual(0, result.exit_code)
        from_config_path.assert_called_once_with(
            config_path=Path("config.yaml"),
            agent_id=None,
            session_id="session-1",
        )

    def test_sessions_delete_deletes_remote_then_local_for_session_agent(self) -> None:
        lookup_service = Mock()
        lookup_service.resume.return_value = Session(
            session_id="session-1",
            agent_id="Pickle",
        )
        delete_service = Mock()
        fake_assembly = Mock()
        fake_assembly.build_session_service.side_effect = [
            lookup_service,
            delete_service,
        ]

        with patch("myopenclaw.cli.main.AppAssembly.from_config_path", return_value=fake_assembly):
            result = self.runner.invoke(
                app,
                ["sessions", "delete", "session-1", "--config", "config.yaml"],
            )

        self.assertEqual(0, result.exit_code)
        self.assertIn("Deleted session session-1", result.stdout)
        fake_assembly.build_session_service.assert_any_call()
        fake_assembly.build_session_service.assert_any_call(agent_id="Pickle")
        delete_service.delete.assert_called_once_with(session_id="session-1")

    def test_sessions_delete_reports_missing_session(self) -> None:
        lookup_service = Mock()
        lookup_service.resume.side_effect = SessionNotFoundError(
            "Session not found: missing"
        )
        fake_assembly = Mock()
        fake_assembly.build_session_service.return_value = lookup_service

        with patch("myopenclaw.cli.main.AppAssembly.from_config_path", return_value=fake_assembly):
            result = self.runner.invoke(
                app,
                ["sessions", "delete", "missing", "--config", "config.yaml"],
            )

        self.assertNotEqual(0, result.exit_code)
        self.assertIn("Session not found: missing", result.stderr)


if __name__ == "__main__":
    unittest.main()
