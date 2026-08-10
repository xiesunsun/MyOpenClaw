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
        digest = hashlib.sha256(payload).hexdigest()
        artifact_id = f"artifact_{digest}"
        existing = self._artifact_store.load_artifact(artifact_id)
        if existing is None:
            blob_key = self._blob_store.put_blob(digest=digest, data=payload)
            artifact = Artifact(
                artifact_id=artifact_id,
                digest=digest,
                media_type=media_type,
                size_bytes=len(payload),
                blob_key=blob_key,
                created_at=self._now(),
            )
            self._artifact_store.insert_artifact(artifact)
        else:
            artifact = existing
            if (
                artifact.digest != digest
                or artifact.media_type != media_type
                or artifact.size_bytes != len(payload)
            ):
                raise ArtifactIntegrityError(
                    f"Artifact 内容或 media_type 冲突: {artifact_id}"
                )
        return ArtifactReference(
            artifact_id=artifact.artifact_id,
            digest=artifact.digest,
            media_type=artifact.media_type,
            size_bytes=artifact.size_bytes,
            display_name=display_name,
        )

    def load_artifact_bytes(self, reference: ArtifactReference) -> bytes:
        artifact = self._artifact_store.load_artifact(reference.artifact_id)
        if artifact is None:
            raise ArtifactIntegrityError(
                f"Artifact 元数据不存在: {reference.artifact_id}"
            )
        if (
            artifact.digest != reference.digest
            or artifact.media_type != reference.media_type
            or artifact.size_bytes != reference.size_bytes
        ):
            raise ArtifactIntegrityError(
                f"ArtifactReference 与元数据不匹配: {reference.artifact_id}"
            )
        data = self._blob_store.load_blob(artifact.blob_key)
        if len(data) != artifact.size_bytes:
            raise ArtifactIntegrityError(
                f"Artifact Blob 大小校验失败: {reference.artifact_id}"
            )
        if hashlib.sha256(data).hexdigest() != artifact.digest:
            raise ArtifactIntegrityError(
                f"Artifact Blob digest 校验失败: {reference.artifact_id}"
            )
        return data
