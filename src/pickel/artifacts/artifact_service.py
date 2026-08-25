"""Artifact 元数据与 Blob 字节的一致性入口。"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import datetime, timezone

from pickel.artifacts.artifact import Artifact, ArtifactReference
from pickel.artifacts.artifact_store import ArtifactStore
from pickel.artifacts.blob_store import BlobStore


class ArtifactIntegrityError(RuntimeError):
    pass


class ArtifactService:
    def __init__(
        self,
        *,
        artifact_store: ArtifactStore,
        blob_store: BlobStore,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._artifact_store = artifact_store
        self._blob_store = blob_store
        self._now = now or (lambda: datetime.now(timezone.utc))

    def create_artifact(
        self,
        *,
        data: bytes,
        media_type: str,
        display_name: str | None = None,
    ) -> ArtifactReference:
        payload = bytes(data)
        artifact_id = f"artifact_{hashlib.sha256(payload).hexdigest()}"
        existing = self._artifact_store.load_artifact(artifact_id)
        if existing is not None:
            if existing.size_bytes != len(payload):
                raise ArtifactIntegrityError(f"Artifact 大小冲突: {artifact_id}")
        # Blob 先于元数据写入；重复写幂等，也可修复元数据存在但 Blob 丢失。
        self._blob_store.put_blob(artifact_id=artifact_id, data=payload)
        if existing is None:
            artifact = Artifact(
                artifact_id=artifact_id,
                size_bytes=len(payload),
                created_at=self._now(),
            )
            self._artifact_store.insert_artifact(artifact)
            existing = artifact
        return ArtifactReference(
            artifact_id=existing.artifact_id,
            media_type=media_type,
            display_name=display_name,
        )

    def load_artifact_bytes(self, reference: ArtifactReference) -> bytes:
        artifact = self._artifact_store.load_artifact(reference.artifact_id)
        if artifact is None:
            raise ArtifactIntegrityError(
                f"Artifact 元数据不存在: {reference.artifact_id}"
            )
        data = self._blob_store.load_blob(reference.artifact_id)
        if len(data) != artifact.size_bytes:
            raise ArtifactIntegrityError(
                f"Artifact Blob 大小校验失败: {reference.artifact_id}"
            )
        if f"artifact_{hashlib.sha256(data).hexdigest()}" != artifact.artifact_id:
            raise ArtifactIntegrityError(
                f"Artifact Blob 内容校验失败: {reference.artifact_id}"
            )
        return data
