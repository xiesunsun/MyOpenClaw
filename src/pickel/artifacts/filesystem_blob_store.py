"""本地文件系统上的内容寻址 BlobStore。"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from tempfile import NamedTemporaryFile

from pickel.artifacts.blob_store import BlobNotFoundError


class FilesystemBlobStore:
    def __init__(self, root: Path) -> None:
        self._root = root.resolve()

    def put_blob(self, *, digest: str, data: bytes) -> str:
        if hashlib.sha256(data).hexdigest() != digest:
            raise ValueError("Blob data 与 digest 不匹配")
        key = self._blob_key(digest)
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            if path.read_bytes() != data:
                raise ValueError(f"Blob digest 冲突: {digest}")
            return key
        with NamedTemporaryFile(dir=path.parent, delete=False) as temporary:
            temporary.write(data)
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_path = Path(temporary.name)
        try:
            os.replace(temporary_path, path)
        finally:
            temporary_path.unlink(missing_ok=True)
        return key

    def load_blob(self, blob_key: str) -> bytes:
        path = self._path(blob_key)
        try:
            return path.read_bytes()
        except FileNotFoundError as exc:
            raise BlobNotFoundError(f"Blob 不存在: {blob_key}") from exc

    def delete_blob(self, blob_key: str) -> None:
        self._path(blob_key).unlink(missing_ok=True)

    @staticmethod
    def _blob_key(digest: str) -> str:
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise ValueError("Blob digest 必须是小写 SHA-256")
        return f"sha256/{digest[:2]}/{digest[2:]}"

    def _path(self, blob_key: str) -> Path:
        relative = Path(blob_key)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"非法 blob_key: {blob_key}")
        path = (self._root / relative).resolve()
        try:
            path.relative_to(self._root)
        except ValueError as exc:
            raise ValueError(f"非法 blob_key: {blob_key}") from exc
        return path
