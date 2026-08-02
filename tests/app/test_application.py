import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from pickel.app.application import RuntimeApplication
from pickel.app.runtime_models import RuntimeLaunchRequest
from pickel.conversations.service import SessionNotFoundError


class RuntimeApplicationLaunchTests(unittest.TestCase):
    def test_session_agent_is_resolved_before_runtime_assembly(self) -> None:
        repository = Mock()
        repository.load.return_value = Mock(agent_id="Pickle")
        application = RuntimeApplication(
            RuntimeLaunchRequest(cwd=Path.cwd(), session_id="session-1")
        )

        with patch(
            "pickel.app.application.SQLiteSessionRepository",
            return_value=repository,
        ):
            agent_ids = application._resolve_launch_agent_ids()

        self.assertEqual(("Pickle",), agent_ids)
        repository.load.assert_called_once_with("session-1")

    def test_missing_session_stops_before_runtime_assembly(self) -> None:
        repository = Mock()
        repository.load.return_value = None
        application = RuntimeApplication(
            RuntimeLaunchRequest(cwd=Path.cwd(), session_id="missing")
        )

        with patch(
            "pickel.app.application.SQLiteSessionRepository",
            return_value=repository,
        ):
            with self.assertRaises(SessionNotFoundError):
                application._resolve_launch_agent_ids()


if __name__ == "__main__":
    unittest.main()
