from pathlib import Path
from unittest.mock import MagicMock

from pickel.cli.chat import ChatLoop
from pickel.cli.slash import BUILTIN_SLASH_COMMANDS


def test_observe_is_registered() -> None:
    command = BUILTIN_SLASH_COMMANDS.get("observe")

    assert command is not None
    assert command.usage == "/observe [path]"


def test_observe_slash_delegates_to_runtime(tmp_path: Path) -> None:
    out = (tmp_path / "custom report.html").resolve()
    loop = object.__new__(ChatLoop)
    loop._conversation = MagicMock()
    loop._conversation.export_observation.return_value = out
    loop._render_system_message = MagicMock()
    loop._render_error_message = MagicMock()

    result = loop._command_observe(str(out))

    assert result is True
    loop._conversation.export_observation.assert_called_once_with(out)
    loop._render_system_message.assert_called_once_with(
        f"Observation report: {out}"
    )
