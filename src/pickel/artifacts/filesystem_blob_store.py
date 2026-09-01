"""本地文件系统上的内容寻址 BlobStore。"""

from __future__ import annotations

import hashlib
import os
import threading
from pathlib import Path
from tempfile import NamedTemporaryFile
from threading import RLock

from pickel.artifacts.blob_store import BlobNotFoundError


class FilesystemBlobStore:
    _locks_guard = threading.Lock()
    _locks: dict[str, RLock] = {}

    def __init__(self, root: Path) -> None:
        self._root = root.resolve()

    @property
    def garbage_collection_lock(self) -> RLock:
        key = str(self._root)
        with self._locks_guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = RLock()
                self._locks[key] = lock
            return lock

    def put_blob(self, *, artifact_id: str, data: bytes) -> None:
        if artifact_id != f"artifact_{hashlib.sha256(data).hexdigest()}":
            raise ValueError("Blob data 与 artifact_id 不匹配")
        with self.garbage_collection_lock:
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
        with self.garbage_collection_lock:
            self._path(artifact_id).unlink(missing_ok=True)

    def list_artifact_ids(self) -> tuple[str, ...]:
        root = self._root / "sha256"
        if not root.is_dir():
            return ()
        with self.garbage_collection_lock:
            return tuple(
                artifact_id
                for prefix in root.iterdir()
                if prefix.is_dir() and len(prefix.name) == 2
                for path in prefix.iterdir()
                if path.is_file() and not path.name.startswith(".tmp-")
                for artifact_id in (f"artifact_{prefix.name}{path.name}",)
                if len(path.name) == 62
                and all(char in "0123456789abcdef" for char in prefix.name + path.name)
            )

    def mtime(self, artifact_id: str) -> float | None:
        try:
            return self._path(artifact_id).stat().st_mtime
        except FileNotFoundError:
            return None

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
