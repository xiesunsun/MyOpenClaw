"""Artifact 元数据与消息内引用。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

_ARTIFACT_ID = re.compile(r"^artifact_[0-9a-f]{64}$")


@dataclass(frozen=True)
class Artifact:
    artifact_id: str
    size_bytes: int
    created_at: datetime

    def __post_init__(self) -> None:
        _validate_id(self.artifact_id)
        if isinstance(self.size_bytes, bool) or self.size_bytes < 0:
            raise ValueError("size_bytes 必须是大于等于 0 的整数")

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "size_bytes": self.size_bytes,
            "created_at": self.created_at.isoformat(),
        }

    def to_json(self) -> str:
        return json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )


@dataclass(frozen=True)
class ArtifactReference:
    artifact_id: str
    media_type: str
    display_name: str | None = None

    def __post_init__(self) -> None:
        _validate_id(self.artifact_id)
        if "/" not in self.media_type or any(
            char.isspace() for char in self.media_type
        ):
            raise ValueError("media_type 必须是有效 MIME type")
        if self.display_name is not None and not self.display_name.strip():
            raise ValueError("display_name 不能为空字符串")

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "media_type": self.media_type,
            "display_name": self.display_name,
        }

    def content_dict(self) -> dict[str, Any]:
        return self.to_dict()

    def to_json(self) -> str:
        return json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )


def artifact_from_json(value: str) -> Artifact:
    data = _object(value, "Artifact")
    _require_keys(data, {"artifact_id", "size_bytes", "created_at"})
    return Artifact(
        artifact_id=_string(data, "artifact_id"),
        size_bytes=_integer(data, "size_bytes"),
        created_at=_time(data, "created_at"),
    )


def artifact_reference_from_dict(content: dict[str, Any]) -> ArtifactReference:
    if not isinstance(content, dict):
        raise TypeError("ArtifactReference 必须是 JSON object")
    _require_keys(content, {"artifact_id", "media_type", "display_name"})
    display_name = content["display_name"]
    if display_name is not None and not isinstance(display_name, str):
        raise TypeError("display_name 必须是字符串或 null")
    return ArtifactReference(
        artifact_id=_string(content, "artifact_id"),
        media_type=_string(content, "media_type"),
        display_name=display_name,
    )


def artifact_reference_from_json(value: str) -> ArtifactReference:
    return artifact_reference_from_dict(_object(value, "ArtifactReference"))


def _validate_id(value: str) -> None:
    if not isinstance(value, str) or not _ARTIFACT_ID.fullmatch(value):
        raise ValueError("artifact_id 必须是 artifact_<64 位小写 SHA-256>")


def _object(value: str, name: str) -> dict[str, Any]:
    try:
        result = json.loads(value)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{name} 必须是合法 JSON object") from exc
    if not isinstance(result, dict):
        raise TypeError(f"{name} 必须是 JSON object")
    return result


def _require_keys(value: dict[str, Any], keys: set[str]) -> None:
    if set(value) != keys:
        raise ValueError(
            f"JSON 字段不匹配，missing={sorted(keys - set(value))}, "
            f"extra={sorted(set(value) - keys)}"
        )


def _string(value: dict[str, Any], key: str) -> str:
    result = value.get(key)
    if not isinstance(result, str):
        raise TypeError(f"{key} 必须是字符串")
    return result


def _integer(value: dict[str, Any], key: str) -> int:
    result = value.get(key)
    if isinstance(result, bool) or not isinstance(result, int):
        raise TypeError(f"{key} 必须是整数")
    return result


def _time(value: dict[str, Any], key: str) -> datetime:
    result = value.get(key)
    if not isinstance(result, str):
        raise TypeError(f"{key} 必须是 ISO8601 字符串")
    try:
        return datetime.fromisoformat(result)
    except ValueError as exc:
        raise ValueError(f"{key} 不是合法 ISO8601 时间") from exc
