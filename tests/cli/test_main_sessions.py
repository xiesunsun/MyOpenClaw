import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from typer.testing import CliRunner

from myopenclaw.cli.main import app
from myopenclaw.conversations.session_preview import SessionPreview


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


if __name__ == "__main__":
    unittest.main()
