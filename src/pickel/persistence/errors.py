"""Persistence 错误的兼容导出；定义位于低层 shared 端口。"""

from __future__ import annotations

from pickel.shared.storage_errors import StorageConflictError, StorageIntegrityError

__all__ = ["StorageConflictError", "StorageIntegrityError"]
