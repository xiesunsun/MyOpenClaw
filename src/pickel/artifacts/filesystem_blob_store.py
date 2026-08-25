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

    def put_blob(self, *, artifact_id: str, data: bytes) -> None:
        if artifact_id != f"artifact_{hashlib.sha256(data).hexdigest()}":
            raise ValueError("Blob data 与 artifact_id 不匹配")
        path = self._path(artifact_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            if path.read_bytes() != data:
                raise ValueError(f"Blob 内容冲突: {artifact_id}")
            return
        with NamedTemporaryFile(dir=path.parent, delete=False) as temporary:
            temporary.write(data)
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_path = Path(temporary.name)
        try:
            os.replace(temporary_path, path)
        finally:
            temporary_path.unlink(missing_ok=True)

    def load_blob(self, artifact_id: str) -> bytes:
        path = self._path(artifact_id)
        try:
            return path.read_bytes()
        except FileNotFoundError as exc:
            raise BlobNotFoundError(f"Blob 不存在: {artifact_id}") from exc

    def delete_blob(self, artifact_id: str) -> None:
        self._path(artifact_id).unlink(missing_ok=True)

    def _path(self, artifact_id: str) -> Path:
        prefix = "artifact_"
        digest = artifact_id.removeprefix(prefix)
        if (
            not artifact_id.startswith(prefix)
            or len(digest) != 64
            or any(char not in "0123456789abcdef" for char in digest)
        ):
            raise ValueError("artifact_id 必须是 artifact_<小写 SHA-256>")
        return self._root / "sha256" / digest[:2] / digest[2:]
