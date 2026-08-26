from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import typer

from pickel.cli import main


class _Boot:
    def __init__(self, service) -> None:
        self.service = service
        self.store = object()
        self.received_store = None

    def runtime_store(self):
        return self.store

    def build_conversation_service(self, *, store):
        self.received_store = store
        return self.service


def test_observe_passes_runtime_store_to_conversation_service(
    monkeypatch, tmp_path: Path
) -> None:
    session = SimpleNamespace(session_id="session-1")
    service = MagicMock()
    service.load_conversation_session.return_value = session
    boot = _Boot(service)
    out = tmp_path / "report.html"
    export = MagicMock(return_value=out)
    monkeypatch.setattr(main, "_boot", lambda: boot)
    monkeypatch.setattr(
        "pickel.observe.operation_report.export_operation_report", export
    )

    main.observe(session=["session-1"], out=out, limit=20)

    assert boot.received_store is boot.store
    export.assert_called_once_with(
        conversation_service=service,
        sessions=(session,),
        out=out,
    )


def test_sessions_and_delete_pass_runtime_store(monkeypatch) -> None:
    service = MagicMock()
    service.list_conversation_previews.return_value = []
    boot = _Boot(service)
    monkeypatch.setattr(main, "_boot", lambda: boot)

    main.sessions(SimpleNamespace(invoked_subcommand=None), all_sessions=True)
    main.delete_session("session-1")

    assert boot.received_store is boot.store
    service.list_conversation_previews.assert_called_once_with(all_sessions=True)
    service.delete_conversation_session.assert_called_once_with(session_id="session-1")


def test_observe_without_exportable_session_still_uses_current_store(
    monkeypatch,
) -> None:
    service = MagicMock()
    service.list_conversation_previews.return_value = []
    boot = _Boot(service)
    monkeypatch.setattr(main, "_boot", lambda: boot)

    with pytest.raises(typer.Exit):
        main.observe(session=[], out=None, limit=20)

    assert boot.received_store is boot.store
