from pathlib import Path
from types import SimpleNamespace

from pickel.app.runtime import RuntimeConversation
from pickel.conversations.agent_message import UserMessage
from pickel.conversations.content_blocks import TextContent
from pickel.conversations.session import Session


class _TraceSink:
    def __init__(self) -> None:
        self.flushed = False
        self.closed = False

    def __call__(self, _event) -> None:
        pass

    def flush(self) -> bool:
        self.flushed = True
        return True

    def close(self) -> None:
        self.closed = True


def test_runtime_exports_current_session_and_flushes_trace(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    session = Session.create(agent_id="Pickle", session_id="session-42")
    session.append_user(UserMessage(content=[TextContent(text="你好")]))
    sink = _TraceSink()
    conversation = RuntimeConversation(
        agent=SimpleNamespace(agent_id="Pickle"),
        run=object(),
        session=session,
        trace_path_resolver=lambda session_id: tmp_path / f"{session_id}.jsonl",
        trace_sink_factory=lambda _path: sink,
    )

    out = conversation.export_observation()

    assert out == tmp_path / "pickel-observe-session-42.html"
    assert sink.flushed is True
    assert sink.closed is False
    assert "session-42" in out.read_text(encoding="utf-8")
    conversation.detach()
