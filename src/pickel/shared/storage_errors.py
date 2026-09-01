"""Store 端口与领域服务共用的窄错误类型。"""

from __future__ import annotations


class StorageConflictError(RuntimeError):
    """调用方给出的 CAS 前置条件已过期。"""


class StorageIntegrityError(RuntimeError):
    """持久化实体或事务违反领域不变量。"""
