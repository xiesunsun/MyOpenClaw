from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from pickel.conversations.conversation_session import ConversationSession
from pickel.model_calls.content_store import FileModelCallContentStore
from pickel.persistence.sqlite_runtime_store import SQLiteRuntimeStore
from pickel.workspaces.workspace import Workspace

UTC = timezone.utc


def _create_empty_session(store: SQLiteRuntimeStore, root: Path) -> None:
    now = datetime(2026, 9, 1, tzinfo=UTC)
    store.create_session(
        workspace=Workspace("workspace-1", root, now),
        session=ConversationSession(
            session_id="session-1",
            agent_id="agent-1",
            workspace_id="workspace-1",
            cwd=root,
            active_node_id=None,
            active_operation_id=None,
            title=None,
            title_source=None,
            created_at=now,
            updated_at=now,
            archived_at=None,
        ),
    )


def test_schema_and_first_store_operation_share_one_connection(
    tmp_path: Path, monkeypatch
) -> None:
    real_connect = sqlite3.connect
    calls = 0

    def counted_connect(*args, **kwargs):
        nonlocal calls
        calls += 1
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", counted_connect)
    root = tmp_path / "workspace"
    root.mkdir()
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")

    _create_empty_session(store, root)
    assert store.load_session("session-1") is not None
    assert store.load_session("session-1") is not None
    # schema 检查和第一次业务写入共用连接；后续调用复用该线程连接。
    assert calls == 1


def test_schema_initialization_is_safe_for_concurrent_store_calls(
    tmp_path: Path,
) -> None:
    path = tmp_path / "runtime.db"
    stores = [SQLiteRuntimeStore(path) for _ in range(8)]

    def load_missing(store: SQLiteRuntimeStore) -> None:
        assert store.load_session("missing") is None

    with ThreadPoolExecutor(max_workers=len(stores)) as executor:
        list(executor.map(load_missing, stores))

    connection = stores[0]._connect()
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 14
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA synchronous").fetchone()[0] == 2
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 30000
    finally:
        stores[0].close()


def test_file_content_put_async_does_not_block_event_loop(tmp_path: Path) -> None:
    store = FileModelCallContentStore(tmp_path / "content")
    original_put = store.put
    started = threading.Event()

    def slow_put(content: bytes):
        started.set()
        time.sleep(0.08)
        return original_put(content)

    store.put = slow_put  # type: ignore[method-assign]

    async def scenario() -> float:
        task = asyncio.create_task(store.put_async(b"large-enough-payload"))
        for _ in range(100):
            if started.is_set():
                break
            await asyncio.sleep(0)
        loop_started = time.perf_counter()
        await asyncio.sleep(0.01)
        elapsed = time.perf_counter() - loop_started
        await task
        return elapsed

    elapsed = asyncio.run(scenario())
    assert elapsed < 0.06
