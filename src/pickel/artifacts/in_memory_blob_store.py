"""测试和临时 Runtime 使用的内存 BlobStore。"""

from __future__ import annotations

import hashlib

from pickel.artifacts.blob_store import BlobNotFoundError


class InMemoryBlobStore:
    def __init__(self) -> None:
        self._blobs: dict[str, bytes] = {}

    def put_blob(self, *, artifact_id: str, data: bytes) -> None:
        expected_id = f"artifact_{hashlib.sha256(data).hexdigest()}"
        if artifact_id != expected_id:
            raise ValueError("Blob data 与 artifact_id 不匹配")
        current = self._blobs.get(artifact_id)
        if current is not None and current != data:
            raise ValueError(f"Blob 内容冲突: {artifact_id}")
        self._blobs[artifact_id] = bytes(data)

    def load_blob(self, artifact_id: str) -> bytes:
        try:
            return bytes(self._blobs[artifact_id])
        except KeyError as exc:
            raise BlobNotFoundError(f"Blob 不存在: {artifact_id}") from exc

    def delete_blob(self, artifact_id: str) -> None:
        self._blobs.pop(artifact_id, None)
