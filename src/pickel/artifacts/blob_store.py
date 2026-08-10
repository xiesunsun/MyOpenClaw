"""二进制内容寻址存储协议。"""

from __future__ import annotations

from typing import Protocol


class BlobNotFoundError(LookupError):
    pass


class BlobStore(Protocol):
    def put_blob(self, *, digest: str, data: bytes) -> str: ...

    def load_blob(self, blob_key: str) -> bytes: ...

    def delete_blob(self, blob_key: str) -> None: ...
