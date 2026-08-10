"""Artifact 的不可变元数据与消息引用。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class Artifact:
    artifact_id: str
    digest: str
    media_type: str
    size_bytes: int
    blob_key: str
    created_at: datetime

    def __post_init__(self) -> None:
        _validate_artifact_fields(
            artifact_id=self.artifact_id,
            digest=self.digest,
            media_type=self.media_type,
            size_bytes=self.size_bytes,
        )
        if not self.blob_key:
            raise ValueError("Artifact.blob_key 不能为空")


@dataclass(frozen=True)
class ArtifactReference:
    """消息内的稳定引用；不携带 Blob 字节或 Provider URL。"""

    artifact_id: str
    digest: str
    media_type: str
    size_bytes: int
    display_name: str | None = None

    def __post_init__(self) -> None:
        _validate_artifact_fields(
            artifact_id=self.artifact_id,
            digest=self.digest,
            media_type=self.media_type,
            size_bytes=self.size_bytes,
        )
        if self.display_name is not None and not self.display_name.strip():
            raise ValueError("ArtifactReference.display_name 不能为空字符串")

    def content_dict(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "digest": self.digest,
            "media_type": self.media_type,
            "size_bytes": self.size_bytes,
            "display_name": self.display_name,
        }


def artifact_reference_from_dict(content: dict[str, Any]) -> ArtifactReference:
    if not isinstance(content, dict):
        raise TypeError("ArtifactReference 必须是 JSON object")
    return ArtifactReference(
        artifact_id=str(content["artifact_id"]),
        digest=str(content["digest"]),
        media_type=str(content["media_type"]),
        size_bytes=int(content["size_bytes"]),
        display_name=(
            str(content["display_name"])
            if content.get("display_name") is not None
            else None
        ),
    )


def _validate_artifact_fields(
    *,
    artifact_id: str,
    digest: str,
    media_type: str,
    size_bytes: int,
) -> None:
    if not artifact_id:
        raise ValueError("artifact_id 不能为空")
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise ValueError("Artifact.digest 必须是小写 SHA-256")
    if "/" not in media_type or any(char.isspace() for char in media_type):
        raise ValueError("Artifact.media_type 必须是有效 MIME type")
    if size_bytes < 0:
        raise ValueError("Artifact.size_bytes 不能小于 0")
