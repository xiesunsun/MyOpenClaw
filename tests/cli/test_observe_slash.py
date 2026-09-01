from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from pickel.cli.chat import ChatLoop
from pickel.cli.slash import BUILTIN_SLASH_COMMANDS


def test_observe_is_registered() -> None:
    command = BUILTIN_SLASH_COMMANDS.get("observe")

    assert command is not None
    assert command.usage == "/observe [port|export [path]]"


def test_observe_slash_starts_dynamic_workspace(monkeypatch) -> None:
    handle = SimpleNamespace(
        session_id="session-1",
        url="http://127.0.0.1:43210/",
        close=MagicMock(),
    )
    start = MagicMock(return_value=handle)
    opened = MagicMock()
    monkeypatch.setattr("pickel.observe.http_server.start_observation_server", start)
    monkeypatch.setattr("pickel.cli.chat.webbrowser.open", opened)

    store = SimpleNamespace(model_call_content_store=object())
    loop = object.__new__(ChatLoop)
    loop._conversation = MagicMock()
    loop._conversation.session = SimpleNamespace(session_id="session-1")
    loop._conversation.persistence_store = store
    loop._observation_server = None
    loop._render_system_message = MagicMock()
    loop._render_error_message = MagicMock()

    result = loop._command_observe(None)

    assert result is True
    start.assert_called_once_with(
        store=store,
        content_store=store.model_call_content_store,
        session_id="session-1",
        port=0,
    )
    opened.assert_called_once_with(handle.url)
    loop._render_system_message.assert_called_once_with(
        f"Observation workspace: {handle.url}"
    )


def test_observe_slash_reuses_current_session_workspace(monkeypatch) -> None:
    handle = SimpleNamespace(
        session_id="session-1",
        url="http://127.0.0.1:43210/",
        close=MagicMock(),
    )
    opened = MagicMock()
    monkeypatch.setattr("pickel.cli.chat.webbrowser.open", opened)

    loop = object.__new__(ChatLoop)
    loop._conversation = MagicMock()
    loop._conversation.session = SimpleNamespace(session_id="session-1")
    loop._observation_server = handle
    loop._render_system_message = MagicMock()
    loop._render_error_message = MagicMock()

    assert loop._command_observe(None) is True

    opened.assert_called_once_with(handle.url)
    handle.close.assert_not_called()


def test_observe_slash_explicit_export_keeps_static_report(tmp_path: Path) -> None:
    out = (tmp_path / "custom report.html").resolve()
    loop = object.__new__(ChatLoop)
    loop._conversation = MagicMock()
    loop._conversation.export_observation.return_value = out
    loop._observation_server = None
    loop._render_system_message = MagicMock()
    loop._render_error_message = MagicMock()

    result = loop._command_observe(f"export {out}")

    assert result is True
    loop._conversation.export_observation.assert_called_once_with(out)
    loop._render_system_message.assert_called_once_with(f"Observation report: {out}")
