"""二进制内容寻址存储协议。"""

from __future__ import annotations

from typing import Protocol


class BlobNotFoundError(LookupError):
    pass


class BlobStore(Protocol):
    def put_blob(self, *, artifact_id: str, data: bytes) -> None: ...

    def load_blob(self, artifact_id: str) -> bytes: ...

    def delete_blob(self, artifact_id: str) -> None: ...
