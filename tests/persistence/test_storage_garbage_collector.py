from __future__ import annotations

import hashlib
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pickel.artifacts.artifact import Artifact, ArtifactReference
from pickel.artifacts.artifact_service import ArtifactService
from pickel.artifacts.filesystem_blob_store import FilesystemBlobStore
from pickel.conversations.agent_message import UserMessage
from pickel.conversations.content_blocks import ArtifactBlock, TextBlock
from pickel.conversations.conversation_node import ConversationNode
from pickel.conversations.conversation_session import ConversationSession
from pickel.model_calls.content_store import FileModelCallContentStore
from pickel.persistence.sqlite_runtime_store import SQLiteRuntimeStore
from pickel.persistence.storage_garbage_collector import StorageGarbageCollector
from pickel.workspaces.workspace import Workspace

UTC = timezone.utc
NOW = datetime(2026, 9, 1, tzinfo=UTC)


def _store(tmp_path: Path) -> SQLiteRuntimeStore:
    root = tmp_path / "workspace"
    root.mkdir()
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    store.create_session(
        workspace=Workspace("workspace-1", root, NOW),
        session=ConversationSession(
            "session-1",
            "agent-1",
            "workspace-1",
            root,
            None,
            None,
            None,
            None,
            NOW,
            NOW,
            None,
        ),
    )
    return store


def _artifact(data: bytes, created_at: datetime) -> Artifact:
    return Artifact(
        artifact_id=f"artifact_{hashlib.sha256(data).hexdigest()}",
        size_bytes=len(data),
        created_at=created_at,
    )


def _age(path: Path, created_at: datetime) -> None:
    timestamp = created_at.timestamp()
    os.utime(path, (timestamp, timestamp))


def test_gc_dry_run_reports_old_orphan_artifact_without_deleting(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    blob_store = FilesystemBlobStore(tmp_path / "blobs")
    artifact = _artifact(b"orphan", NOW - timedelta(days=3))
    blob_store.put_blob(artifact_id=artifact.artifact_id, data=b"orphan")
    _age(blob_store._path(artifact.artifact_id), artifact.created_at)
    store.insert_artifact(artifact)

    result = StorageGarbageCollector(
        store,
        blob_store=blob_store,
        now=lambda: NOW,
        grace_period=timedelta(days=1),
    ).collect()

    assert result.dry_run
    assert result.orphan_artifact_ids == (artifact.artifact_id,)
    assert result.deleted_count == 0
    assert store.load_artifact(artifact.artifact_id) == artifact
    assert blob_store.load_blob(artifact.artifact_id) == b"orphan"


def test_gc_execute_removes_old_orphan_artifact_and_blob(tmp_path: Path) -> None:
    store = _store(tmp_path)
    blob_store = FilesystemBlobStore(tmp_path / "blobs")
    artifact = _artifact(b"orphan", NOW - timedelta(days=3))
    blob_store.put_blob(artifact_id=artifact.artifact_id, data=b"orphan")
    _age(blob_store._path(artifact.artifact_id), artifact.created_at)
    store.insert_artifact(artifact)

    result = StorageGarbageCollector(
        store,
        blob_store=blob_store,
        now=lambda: NOW,
        grace_period=timedelta(days=1),
    ).collect(execute=True)

    assert result.deleted_artifact_ids == (artifact.artifact_id,)
    assert result.deleted_blob_ids == (artifact.artifact_id,)
    assert store.load_artifact(artifact.artifact_id) is None
    assert not blob_store._path(artifact.artifact_id).exists()


def test_gc_keeps_artifact_referenced_by_conversation_node(tmp_path: Path) -> None:
    store = _store(tmp_path)
    blob_store = FilesystemBlobStore(tmp_path / "blobs")
    data = b"referenced"
    artifact = _artifact(data, NOW - timedelta(days=3))
    blob_store.put_blob(artifact_id=artifact.artifact_id, data=data)
    _age(blob_store._path(artifact.artifact_id), artifact.created_at)
    store.insert_artifact(artifact)
    node = ConversationNode(
        node_id="node-1",
        session_id="session-1",
        parent_node_id=None,
        content_type="agent_message",
        content=UserMessage(
            (
                TextBlock("see file"),
                ArtifactBlock(
                    ArtifactReference(artifact.artifact_id, "application/octet-stream")
                ),
            )
        ),
        created_at=NOW,
    )
    assert store.append_node(node=node, expected_node_id=None)

    result = StorageGarbageCollector(
        store,
        blob_store=blob_store,
        now=lambda: NOW,
        grace_period=timedelta(days=1),
    ).collect(execute=True)

    assert result.referenced_artifact_ids == (artifact.artifact_id,)
    assert result.orphan_artifact_ids == ()
    assert store.load_artifact(artifact.artifact_id) == artifact
    assert blob_store.load_blob(artifact.artifact_id) == data


def test_gc_keeps_recent_orphan_content_during_grace_period(tmp_path: Path) -> None:
    store = _store(tmp_path)
    content_store = FileModelCallContentStore(tmp_path / "content")
    ref = content_store.put(b"recent orphan")
    _age(content_store._path(ref), NOW)

    result = StorageGarbageCollector(
        store,
        content_store=content_store,
        now=lambda: NOW,
        grace_period=timedelta(hours=1),
    ).collect(execute=True)

    assert result.orphan_content_digests == ()
    assert content_store.get(ref) == b"recent orphan"


def test_gc_keeps_model_call_content_referenced_by_sqlite(tmp_path: Path) -> None:
    store = _store(tmp_path)
    content_store = FileModelCallContentStore(tmp_path / "content")
    ref = content_store.put(b"referenced request")
    _age(content_store._path(ref), NOW - timedelta(days=3))
    connection = store._connect()
    connection.execute(
        """
        INSERT INTO model_calls (
            model_call_id, session_id, operation_id, step_id, step_sequence,
            request_attempt, model_role, purpose, provider, api_kind, endpoint,
            requested_model, returned_model, status, request_content_ref,
            response_content_ref, context_fingerprint, provider_request_id,
            http_status, error_json, created_at, started_at, first_chunk_at,
            finished_at
        ) VALUES (?, ?, NULL, NULL, NULL, 1, 'worker', 'history_compaction',
                  'test', 'test', 'test', 'test', NULL, 'prepared', ?, NULL,
                  NULL, NULL, NULL, NULL, ?, NULL, NULL, NULL)
        """,
        ("call-1", "session-1", ref.to_string(), NOW.isoformat()),
    )
    connection.commit()

    result = StorageGarbageCollector(
        store,
        content_store=content_store,
        now=lambda: NOW,
        grace_period=timedelta(days=1),
    ).collect(execute=True)

    assert result.referenced_content_digests == (ref.sha256,)
    assert result.orphan_content_digests == ()
    assert content_store.get(ref) == b"referenced request"


def test_artifact_service_and_gc_share_blob_write_lock(tmp_path: Path) -> None:
    store = _store(tmp_path)
    blob_store = FilesystemBlobStore(tmp_path / "blobs")
    service = ArtifactService(
        artifact_store=store, blob_store=blob_store, now=lambda: NOW
    )
    artifact = _artifact(b"raced", NOW - timedelta(days=3))
    blob_store.put_blob(artifact_id=artifact.artifact_id, data=b"raced")
    _age(blob_store._path(artifact.artifact_id), artifact.created_at)
    store.insert_artifact(artifact)
    collector = StorageGarbageCollector(
        store,
        blob_store=blob_store,
        now=lambda: NOW,
        grace_period=timedelta(days=1),
    )
    entered = threading.Event()
    release = threading.Event()
    original_delete = collector._delete_artifacts

    def pause_before_blob_delete(artifact_ids):
        entered.set()
        assert release.wait(timeout=2)
        return original_delete(artifact_ids)

    collector._delete_artifacts = pause_before_blob_delete  # type: ignore[method-assign]
    gc_thread = threading.Thread(target=lambda: collector.collect(execute=True))
    gc_thread.start()
    assert entered.wait(timeout=2)

    created: list[ArtifactReference] = []
    writer = threading.Thread(
        target=lambda: created.append(
            service.create_artifact(
                data=b"raced", media_type="application/octet-stream"
            )
        )
    )
    writer.start()
    # GC 持有 Blob 根目录锁时，新的 put→insert 不能穿过物理删除窗口。
    writer.join(timeout=0.02)
    assert writer.is_alive()
    release.set()
    gc_thread.join(timeout=2)
    writer.join(timeout=2)
    assert not gc_thread.is_alive()
    assert not writer.is_alive()
    assert created and store.load_artifact(created[0].artifact_id) is not None
    assert blob_store.load_blob(created[0].artifact_id) == b"raced"
