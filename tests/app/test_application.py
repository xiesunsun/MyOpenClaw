from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from pickel.app.application import RuntimeApplication
from pickel.app.runtime_models import RuntimeLaunchRequest
from pickel.conversations.conversation_service import ConversationNotFoundError


def test_session_agent_is_resolved_from_runtime_store() -> None:
    store = Mock()
    store.load_session.return_value = Mock(agent_id="Pickle")
    application = RuntimeApplication(
        RuntimeLaunchRequest(cwd=Path.cwd(), session_id="session-1")
    )

    with patch("pickel.app.application.SQLiteRuntimeStore", return_value=store):
        agent_ids = application._resolve_launch_agent_ids()

    assert agent_ids == ("Pickle",)
    store.load_session.assert_called_once_with("session-1")


def test_missing_session_stops_before_runtime_assembly() -> None:
    store = Mock()
    store.load_session.return_value = None
    application = RuntimeApplication(
        RuntimeLaunchRequest(cwd=Path.cwd(), session_id="missing")
    )

    with patch("pickel.app.application.SQLiteRuntimeStore", return_value=store):
        with pytest.raises(ConversationNotFoundError):
            application._resolve_launch_agent_ids()
