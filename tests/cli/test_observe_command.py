"""pickel observe:从真实 SQLite 会话库导出 HTML。"""

from datetime import datetime, timezone

from typer.testing import CliRunner

from pickel.cli.main import app
from pickel.conversations.agent_message import (
    AssistantMessage,
    ModelResponseMetadata,
    ModelUsage,
    UserMessage,
)
from pickel.conversations.content_blocks import TextContent
from pickel.conversations.session import Session
from pickel.persistence.sqlite_session_repository import SQLiteSessionRepository


def _seed_session(home) -> str:
    repository = SQLiteSessionRepository(home / "sessions.db")
    session = Session.create(agent_id="Pickle", cwd="/tmp")
    repository.create(session)
    session.append_user(UserMessage(content=[TextContent(text="你好")]))
    session.append_assistant(
        AssistantMessage(
            content=[TextContent(text="你好!")],
            metadata=ModelResponseMetadata(
                provider="anthropic",
                model="claude-sonnet-5",
                elapsed_ms=700,
                usage=ModelUsage(input_tokens=100, output_tokens=10),
            ),
        )
    )
    repository.append_entries(
        session_id=session.session_id,
        entries=session.entries,
        leaf_id=session.leaf_id,
        updated_at=datetime.now(timezone.utc),
    )
    return session.session_id


def test_observe_exports_html(tmp_path, monkeypatch):
    monkeypatch.setenv("PICKEL_HOME", str(tmp_path))
    session_id = _seed_session(tmp_path)
    out = tmp_path / "report.html"

    result = CliRunner().invoke(app, ["observe", "--out", str(out)])

    assert result.exit_code == 0, result.output
    assert out.exists()
    content = out.read_text(encoding="utf-8")
    assert session_id in content
    assert str(out.resolve()) in result.output


def test_observe_empty_db_exits_nonzero(tmp_path, monkeypatch):
    monkeypatch.setenv("PICKEL_HOME", str(tmp_path))
    out = tmp_path / "report.html"

    result = CliRunner().invoke(app, ["observe", "--out", str(out)])

    assert result.exit_code == 1
    assert not out.exists()
