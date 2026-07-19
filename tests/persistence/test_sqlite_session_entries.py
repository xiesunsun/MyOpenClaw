"""SQLite session_entries 原子 append 与 active_path 恢复。"""

from __future__ import annotations

import sqlite3

from myopenclaw.conversations.agent_message import UserMessage
from myopenclaw.conversations.content_blocks import TextContent
from myopenclaw.conversations.session import Session
from myopenclaw.persistence.sqlite_session_repository import SQLiteSessionRepository


def test_create_append_reload_active_path(tmp_path):
    repo = SQLiteSessionRepository(tmp_path / "s.db")
    session = Session.create(agent_id="Pickle")
    repo.create(session)
    session.append_user(UserMessage(content=[TextContent(text="hi")]))
    repo.append_entries(
        session_id=session.session_id,
        entries=session.entries[-1:],
        leaf_id=session.leaf_id,
        updated_at=session.updated_at,
    )
    loaded = repo.load(session.session_id)
    assert loaded is not None
    assert len(loaded.active_path()) == 1
    assert loaded.leaf_id == session.leaf_id


def test_append_entry_and_leaf_are_atomic(tmp_path, monkeypatch):
    """UPDATE sessions 失败时不得留下半写入 entry；leaf 保持不变。"""
    repo = SQLiteSessionRepository(tmp_path / "s.db")
    session = Session.create(agent_id="Pickle")
    repo.create(session)
    session.append_user(UserMessage(content=[TextContent(text="hi")]))
    entry = session.entries[-1]

    real_connect = repo._connect

    class BoomConnection:
        def __init__(self, inner):
            self._inner = inner

        def __enter__(self):
            self._inner.__enter__()
            return self

        def __exit__(self, *args):
            return self._inner.__exit__(*args)

        def execute(self, sql, params=None):
            if sql.strip().upper().startswith("UPDATE") and "sessions" in sql.lower():
                raise sqlite3.OperationalError("simulated leaf update failure")
            if params is None:
                return self._inner.execute(sql)
            return self._inner.execute(sql, params)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    def fake_connect():
        return BoomConnection(real_connect())

    monkeypatch.setattr(repo, "_connect", fake_connect)
    raised = False
    try:
        repo.append_entries(
            session_id=session.session_id,
            entries=[entry],
            leaf_id=entry.entry_id,
            updated_at=session.updated_at,
        )
    except Exception:
        raised = True
    assert raised

    monkeypatch.setattr(repo, "_connect", real_connect)
    loaded = repo.load(session.session_id)
    assert loaded is not None
    assert loaded.leaf_id is None
    assert loaded.entries == []


def test_schema_user_version_is_2(tmp_path):
    repo = SQLiteSessionRepository(tmp_path / "s.db")
    session = Session.create(agent_id="Pickle")
    repo.create(session)

    with sqlite3.connect(tmp_path / "s.db") as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        indexes = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            ).fetchall()
        }

    assert version == 2
    assert "sessions" in tables
    assert "session_entries" in tables
    assert "idx_session_entries_session_parent" in indexes
    assert "idx_session_entries_session_created" in indexes
    assert "idx_sessions_agent_updated" in indexes
