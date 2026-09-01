"""SQLite Runtime 内容与 Artifact 的保守垃圾回收。"""

from __future__ import annotations

import json
import re
from contextlib import ExitStack, nullcontext
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from pickel.model_calls.content_store import ModelCallContentRef
from pickel.persistence.sqlite_runtime_store import SQLiteRuntimeStore

_ARTIFACT_ID = re.compile(r"^artifact_[0-9a-f]{64}$")
_DEFAULT_GRACE_PERIOD = timedelta(hours=24)


@dataclass(frozen=True)
class StorageGarbageCollectionResult:
    """一次 GC 的候选和实际删除结果。"""

    dry_run: bool
    cutoff: datetime
    referenced_artifact_ids: tuple[str, ...]
    orphan_artifact_ids: tuple[str, ...]
    deleted_artifact_ids: tuple[str, ...]
    referenced_content_digests: tuple[str, ...]
    orphan_content_digests: tuple[str, ...]
    deleted_content_digests: tuple[str, ...]
    orphan_blob_ids: tuple[str, ...]
    deleted_blob_ids: tuple[str, ...]

    @property
    def candidate_count(self) -> int:
        return (
            len(self.orphan_artifact_ids)
            + len(self.orphan_content_digests)
            + len(self.orphan_blob_ids)
        )

    @property
    def deleted_count(self) -> int:
        return (
            len(self.deleted_artifact_ids)
            + len(self.deleted_content_digests)
            + len(self.deleted_blob_ids)
        )


class StorageGarbageCollector:
    """扫描 SQLite 引用并回收过期的 Artifact、Blob 和 ModelCall 内容。

    ``collect()`` 默认只返回候选，不产生删除。执行模式先锁定物理内容根目录，
    再以 SQLite 写事务重新扫描引用，避免本进程的 ``put → insert`` 提交链与
    删除交错。物理文件没有数据库创建时间，因此使用文件 mtime 作为宽限期门槛。
    """

    def __init__(
        self,
        runtime_store: SQLiteRuntimeStore,
        *,
        content_store: Any | None = None,
        blob_store: Any | None = None,
        grace_period: timedelta = _DEFAULT_GRACE_PERIOD,
        now: Any | None = None,
    ) -> None:
        if grace_period.total_seconds() < 0:
            raise ValueError("GC grace_period 不能小于 0")
        self._runtime_store = runtime_store
        self._content_store = content_store or runtime_store.model_call_content_store
        self._blob_store = blob_store
        self._grace_period = grace_period
        self._now = now or (lambda: datetime.now(timezone.utc))

    def collect(
        self,
        *,
        execute: bool = False,
        grace_period: timedelta | None = None,
    ) -> StorageGarbageCollectionResult:
        """返回本轮候选；只有 ``execute=True`` 才删除。"""
        period = grace_period if grace_period is not None else self._grace_period
        if period.total_seconds() < 0:
            raise ValueError("GC grace_period 不能小于 0")
        now = self._now()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        cutoff = now - period

        with ExitStack() as locks:
            if execute:
                self._enter_storage_lock(locks, self._content_store)
                self._enter_storage_lock(locks, self._blob_store)
            self._runtime_store._ensure_schema()
            connection = self._runtime_store._connect()
            if execute:
                connection.execute("BEGIN IMMEDIATE")
            try:
                result, artifacts, content, blobs = self._collect_candidates(
                    connection, cutoff=cutoff, execute=execute
                )
                if execute:
                    connection.commit()
                    deleted_artifacts = self._delete_artifacts(artifacts)
                    deleted_content = self._delete_content(content)
                    deleted_blobs = self._delete_blobs(blobs)
                    return StorageGarbageCollectionResult(
                        **{
                            **result.__dict__,
                            "deleted_artifact_ids": tuple(deleted_artifacts),
                            "deleted_content_digests": tuple(deleted_content),
                            "deleted_blob_ids": tuple(deleted_blobs),
                        }
                    )
                return result
            except Exception:
                if execute and connection.in_transaction:
                    connection.rollback()
                raise

    @staticmethod
    def _enter_storage_lock(stack: ExitStack, storage: Any | None) -> None:
        lock = getattr(storage, "garbage_collection_lock", None)
        stack.enter_context(lock if lock is not None else nullcontext())

    def _collect_candidates(
        self,
        connection,
        *,
        cutoff: datetime,
        execute: bool,
    ) -> tuple[
        StorageGarbageCollectionResult,
        tuple[str, ...],
        tuple[ModelCallContentRef, ...],
        tuple[str, ...],
    ]:
        artifact_refs, protect_artifacts = _scan_artifact_references(connection)
        content_refs, protect_content = _scan_content_references(connection)
        artifact_rows = connection.execute(
            "SELECT artifact_id, created_at FROM artifacts"
        ).fetchall()
        artifact_ids = {str(row["artifact_id"]) for row in artifact_rows}
        old_artifacts = {
            str(row["artifact_id"])
            for row in artifact_rows
            if _parse_time(row["created_at"]) <= cutoff
        }
        orphan_artifacts = set() if protect_artifacts else old_artifacts - artifact_refs
        if execute and orphan_artifacts:
            marks = ",".join("?" for _ in orphan_artifacts)
            connection.execute(
                f"DELETE FROM artifacts WHERE artifact_id IN ({marks})",
                tuple(sorted(orphan_artifacts)),
            )

        content_candidates = self._old_content_candidates(
            cutoff, content_refs, protect_content
        )
        blob_candidates = self._old_blob_candidates(
            cutoff,
            artifact_refs,
            artifact_ids,
            orphan_artifacts,
            protect_artifacts,
        )
        result = StorageGarbageCollectionResult(
            dry_run=not execute,
            cutoff=cutoff,
            referenced_artifact_ids=tuple(sorted(artifact_refs)),
            orphan_artifact_ids=tuple(sorted(orphan_artifacts)),
            deleted_artifact_ids=(),
            referenced_content_digests=tuple(sorted(content_refs)),
            orphan_content_digests=tuple(ref.sha256 for ref in content_candidates),
            deleted_content_digests=(),
            orphan_blob_ids=tuple(sorted(blob_candidates)),
            deleted_blob_ids=(),
        )
        return (
            result,
            tuple(sorted(orphan_artifacts)),
            content_candidates,
            blob_candidates,
        )

    def _old_content_candidates(
        self,
        cutoff: datetime,
        referenced: set[str],
        protect_all: bool,
    ) -> tuple[ModelCallContentRef, ...]:
        if protect_all or not hasattr(self._content_store, "list_refs"):
            return ()
        candidates: list[ModelCallContentRef] = []
        for ref in self._content_store.list_refs():
            mtime = self._content_store.mtime(ref)
            if (
                ref.sha256 not in referenced
                and mtime is not None
                and datetime.fromtimestamp(mtime, timezone.utc) <= cutoff
            ):
                candidates.append(ref)
        return tuple(sorted(candidates, key=lambda ref: ref.sha256))

    def _old_blob_candidates(
        self,
        cutoff: datetime,
        artifact_refs: set[str],
        artifact_rows: set[str],
        orphan_artifacts: set[str],
        protect_all: bool,
    ) -> tuple[str, ...]:
        if (
            protect_all
            or self._blob_store is None
            or not hasattr(self._blob_store, "list_artifact_ids")
        ):
            return ()
        candidates: list[str] = []
        for artifact_id in self._blob_store.list_artifact_ids():
            if artifact_id in artifact_refs:
                continue
            if artifact_id in artifact_rows and artifact_id not in orphan_artifacts:
                continue
            mtime = self._blob_store.mtime(artifact_id)
            if (
                mtime is not None
                and datetime.fromtimestamp(mtime, timezone.utc) <= cutoff
            ):
                candidates.append(artifact_id)
        return tuple(sorted(candidates))

    def _delete_artifacts(self, artifact_ids: tuple[str, ...]) -> tuple[str, ...]:
        # 元数据已在 SQLite 事务中删除；Blob 删除单独 best effort，保留失败孤儿供下轮重试。
        return artifact_ids

    def _delete_content(self, refs: tuple[ModelCallContentRef, ...]) -> tuple[str, ...]:
        deleted: list[str] = []
        for ref in refs:
            self._content_store.delete(ref)
            deleted.append(ref.sha256)
        return tuple(deleted)

    def _delete_blobs(self, artifact_ids: tuple[str, ...]) -> tuple[str, ...]:
        if self._blob_store is None:
            return ()
        deleted: list[str] = []
        for artifact_id in artifact_ids:
            self._blob_store.delete_blob(artifact_id)
            deleted.append(artifact_id)
        return tuple(deleted)


def _scan_artifact_references(connection) -> tuple[set[str], bool]:
    references: set[str] = set()
    protect_all = False
    for row in connection.execute("SELECT content_json FROM conversation_nodes"):
        value, valid = _decode_json(row["content_json"])
        if not valid:
            protect_all = True
        else:
            _find_artifact_ids(value, references)
    for row in connection.execute("""
        SELECT current_step_json FROM agent_run_states
        WHERE status NOT IN ('succeeded', 'failed', 'cancelled')
          AND current_step_json IS NOT NULL
        """):
        value, valid = _decode_json(row["current_step_json"])
        if not valid:
            protect_all = True
            continue
        intent = value.get("request_intent") if isinstance(value, dict) else None
        if isinstance(intent, dict):
            _find_artifact_ids(intent.get("model_context"), references)
    return references, protect_all


def _scan_content_references(connection) -> tuple[set[str], bool]:
    references: set[str] = set()
    protect_all = False
    for row in connection.execute(
        "SELECT request_content_ref, response_content_ref FROM model_calls"
    ):
        for raw in (row["request_content_ref"], row["response_content_ref"]):
            if raw is None:
                continue
            try:
                data = json.loads(str(raw))
                digest = data.get("sha256") if isinstance(data, dict) else None
            except (TypeError, json.JSONDecodeError):
                protect_all = True
                continue
            if not isinstance(digest, str) or len(digest) != 64:
                protect_all = True
                continue
            if all(char in "0123456789abcdef" for char in digest):
                references.add(digest)
            else:
                protect_all = True
    return references, protect_all


def _find_artifact_ids(value: Any, references: set[str]) -> None:
    if isinstance(value, dict):
        artifact_id = value.get("artifact_id")
        if isinstance(artifact_id, str) and _ARTIFACT_ID.fullmatch(artifact_id):
            references.add(artifact_id)
        for child in value.values():
            _find_artifact_ids(child, references)
    elif isinstance(value, list):
        for child in value:
            _find_artifact_ids(child, references)


def _decode_json(value: Any) -> tuple[Any, bool]:
    try:
        return json.loads(str(value)), True
    except (TypeError, json.JSONDecodeError):
        return None, False


def _parse_time(value: Any) -> datetime:
    parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


__all__ = ["StorageGarbageCollector", "StorageGarbageCollectionResult"]
