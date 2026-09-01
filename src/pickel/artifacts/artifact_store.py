"""Artifact 元数据的窄持久化协议。"""

from __future__ import annotations

from typing import Protocol

from pickel.artifacts.artifact import Artifact


class ArtifactStore(Protocol):
    def insert_artifact(self, artifact: Artifact) -> None: ...

    def load_artifact(self, artifact_id: str) -> Artifact | None: ...
