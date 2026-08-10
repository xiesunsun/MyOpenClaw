"""Agent Runtime 组合根所需的完整持久化协议。"""

from __future__ import annotations

from typing import Protocol

from pickel.artifacts.artifact_store import ArtifactStore
from pickel.operations.operation_store import OperationStore


class RuntimeStore(OperationStore, ArtifactStore, Protocol):
    """仅供 Composition Root 组合；领域服务继续依赖更窄协议。"""

    pass
