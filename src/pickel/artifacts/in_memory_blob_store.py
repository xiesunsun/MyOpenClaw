"""测试和临时 Runtime 使用的内存 BlobStore。"""

from __future__ import annotations

import hashlib

from pickel.artifacts.blob_store import BlobNotFoundError


class InMemoryBlobStore:
    def __init__(self) -> None:
        self._blobs: dict[str, bytes] = {}

    def put_blob(self, *, digest: str, data: bytes) -> str:
        if hashlib.sha256(data).hexdigest() != digest:
            raise ValueError("Blob data 与 digest 不匹配")
        key = f"sha256/{digest}"
        current = self._blobs.get(key)
        if current is not None and current != data:
            raise ValueError(f"Blob digest 冲突: {digest}")
        self._blobs[key] = bytes(data)
        return key

    def load_blob(self, blob_key: str) -> bytes:
        try:
            return bytes(self._blobs[blob_key])
        except KeyError as exc:
            raise BlobNotFoundError(f"Blob 不存在: {blob_key}") from exc

    def delete_blob(self, blob_key: str) -> None:
        self._blobs.pop(blob_key, None)
