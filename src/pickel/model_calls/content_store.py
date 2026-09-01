"""ModelCall 内容寻址存储。"""

from __future__ import annotations

import hashlib
import asyncio
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
import threading
from threading import RLock
from typing import Protocol

MEDIA_TYPE = "application/vnd.pickel.model-call+json"
ENCODING = "utf-8"


class ModelCallContentError(RuntimeError):
    """ModelCall 内容存储读写失败。"""


class ModelCallContentMissingError(ModelCallContentError):
    """已引用的 ModelCall 内容不存在。"""


class ModelCallContentCorruptError(ModelCallContentError):
    """ModelCall 内容与引用中的摘要或大小不一致。"""


@dataclass(frozen=True)
class ModelCallContentRef:
    sha256: str
    media_type: str
    encoding: str
    size_bytes: int

    def __post_init__(self) -> None:
        if len(self.sha256) != 64 or any(
            char not in "0123456789abcdef" for char in self.sha256
        ):
            raise ValueError("ModelCallContentRef.sha256 必须是小写 SHA-256")
        if self.media_type != MEDIA_TYPE:
            raise ValueError(
                f"不支持的 ModelCall content media type: {self.media_type}"
            )
        if self.encoding != ENCODING:
            raise ValueError(f"不支持的 ModelCall content encoding: {self.encoding}")
        if self.size_bytes < 0:
            raise ValueError("ModelCallContentRef.size_bytes 不能小于 0")

    def to_string(self) -> str:
        return json.dumps(
            {
                "encoding": self.encoding,
                "media_type": self.media_type,
                "sha256": self.sha256,
                "size_bytes": self.size_bytes,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )

    @classmethod
    def from_string(cls, value: str) -> "ModelCallContentRef":
        try:
            data = json.loads(value)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("ModelCallContentRef 必须是合法 JSON object") from exc
        if not isinstance(data, dict) or set(data) != {
            "encoding",
            "media_type",
            "sha256",
            "size_bytes",
        }:
            raise ValueError("ModelCallContentRef 字段不完整")
        size = data["size_bytes"]
        if isinstance(size, bool) or not isinstance(size, int):
            raise TypeError("ModelCallContentRef.size_bytes 必须是 integer")
        return cls(
            sha256=str(data["sha256"]),
            media_type=str(data["media_type"]),
            encoding=str(data["encoding"]),
            size_bytes=size,
        )


class ModelCallContentStore(Protocol):
    def put(self, content: bytes) -> ModelCallContentRef: ...

    async def put_async(self, content: bytes) -> ModelCallContentRef: ...

    def get(self, ref: ModelCallContentRef) -> bytes: ...

    def exists(self, ref: ModelCallContentRef) -> bool: ...

    def delete(self, ref: ModelCallContentRef) -> None: ...


class FileModelCallContentStore:
    """同目录临时文件 + fsync + 原子 rename 的本地内容存储。"""

    _locks_guard = threading.Lock()
    _locks: dict[str, RLock] = {}

    def __init__(self, root: Path) -> None:
        self._root = Path(root)

    @property
    def garbage_collection_lock(self) -> RLock:
        key = str(self._root.expanduser().resolve(strict=False))
        with self._locks_guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = RLock()
                self._locks[key] = lock
            return lock

    @property
    def root(self) -> Path:
        return self._root

    def put(self, content: bytes) -> ModelCallContentRef:
        value = bytes(content)
        ref = _ref_for(value)
        path = self._path(ref)
        with self.garbage_collection_lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists():
                self._verify(ref, path.read_bytes())
                return ref

            fd, temp_name = tempfile.mkstemp(prefix=".tmp-", dir=path.parent)
            temp_path = Path(temp_name)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(value)
                    stream.flush()
                    os.fsync(stream.fileno())
                if path.exists():
                    self._verify(ref, path.read_bytes())
                    temp_path.unlink(missing_ok=True)
                    return ref
                os.replace(temp_path, path)
                _fsync_directory(path.parent)
            except Exception:
                temp_path.unlink(missing_ok=True)
                raise
            return ref

    async def put_async(self, content: bytes) -> ModelCallContentRef:
        """将带 fsync 的大文件写入移出 asyncio 事件循环。"""
        return await asyncio.to_thread(self.put, content)

    def get(self, ref: ModelCallContentRef) -> bytes:
        path = self._path(ref)
        try:
            content = path.read_bytes()
        except FileNotFoundError as exc:
            raise ModelCallContentMissingError(
                f"ModelCall 内容不存在: {ref.sha256}"
            ) from exc
        self._verify(ref, content)
        return content

    def exists(self, ref: ModelCallContentRef) -> bool:
        path = self._path(ref)
        if not path.is_file():
            return False
        try:
            self._verify(ref, path.read_bytes())
        except ModelCallContentCorruptError:
            return False
        return True

    def delete(self, ref: ModelCallContentRef) -> None:
        with self.garbage_collection_lock:
            self._path(ref).unlink(missing_ok=True)

    def list_refs(self) -> tuple[ModelCallContentRef, ...]:
        """列出可供 GC 检查的完整内容文件；临时文件不纳入候选。"""
        root = self._root / "sha256"
        if not root.is_dir():
            return ()
        refs: list[ModelCallContentRef] = []
        with self.garbage_collection_lock:
            for prefix in root.iterdir():
                if not prefix.is_dir() or len(prefix.name) != 2:
                    continue
                for path in prefix.iterdir():
                    if path.name.startswith(".tmp-") or not path.is_file():
                        continue
                    digest = prefix.name + path.name
                    if len(digest) != 64 or any(
                        char not in "0123456789abcdef" for char in digest
                    ):
                        continue
                    try:
                        size = path.stat().st_size
                    except FileNotFoundError:
                        continue
                    refs.append(
                        ModelCallContentRef(
                            sha256=digest,
                            media_type=MEDIA_TYPE,
                            encoding=ENCODING,
                            size_bytes=size,
                        )
                    )
        return tuple(refs)

    def mtime(self, ref: ModelCallContentRef) -> float | None:
        try:
            return self._path(ref).stat().st_mtime
        except FileNotFoundError:
            return None

    def _path(self, ref: ModelCallContentRef) -> Path:
        return self._root / "sha256" / ref.sha256[:2] / ref.sha256[2:]

    @staticmethod
    def _verify(ref: ModelCallContentRef, content: bytes) -> None:
        if len(content) != ref.size_bytes or _digest(content) != ref.sha256:
            raise ModelCallContentCorruptError(
                f"ModelCall 内容摘要或大小不匹配: {ref.sha256}"
            )


class InMemoryModelCallContentStore:
    """测试与纯内存 Runtime 使用的等价内容寻址实现。"""

    def __init__(self) -> None:
        self._contents: dict[str, bytes] = {}
        self._lock = RLock()

    def put(self, content: bytes) -> ModelCallContentRef:
        value = bytes(content)
        ref = _ref_for(value)
        with self._lock:
            existing = self._contents.get(ref.sha256)
            if existing is not None and existing != value:
                raise ModelCallContentCorruptError(
                    f"ModelCall 内容摘要冲突: {ref.sha256}"
                )
            self._contents[ref.sha256] = value
        return ref

    async def put_async(self, content: bytes) -> ModelCallContentRef:
        # 保持与文件实现相同的窄 async 接口；内存写入很快，但 offload 让
        # 调用方无需按实现类型分支，也方便测试替身模拟慢写入。
        return await asyncio.to_thread(self.put, content)

    def get(self, ref: ModelCallContentRef) -> bytes:
        with self._lock:
            value = self._contents.get(ref.sha256)
        if value is None:
            raise ModelCallContentMissingError(f"ModelCall 内容不存在: {ref.sha256}")
        if len(value) != ref.size_bytes or _digest(value) != ref.sha256:
            raise ModelCallContentCorruptError(
                f"ModelCall 内容摘要或大小不匹配: {ref.sha256}"
            )
        return value

    def exists(self, ref: ModelCallContentRef) -> bool:
        try:
            self.get(ref)
        except (ModelCallContentMissingError, ModelCallContentCorruptError):
            return False
        return True

    def delete(self, ref: ModelCallContentRef) -> None:
        with self._lock:
            self._contents.pop(ref.sha256, None)

    def list_refs(self) -> tuple[ModelCallContentRef, ...]:
        with self._lock:
            return tuple(
                ModelCallContentRef(
                    sha256=digest,
                    media_type=MEDIA_TYPE,
                    encoding=ENCODING,
                    size_bytes=len(content),
                )
                for digest, content in self._contents.items()
            )

    def mtime(self, ref: ModelCallContentRef) -> float | None:
        del ref
        return None


def _ref_for(content: bytes) -> ModelCallContentRef:
    return ModelCallContentRef(
        sha256=_digest(content),
        media_type=MEDIA_TYPE,
        encoding=ENCODING,
        size_bytes=len(content),
    )


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
