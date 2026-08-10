"""创建后不可修改的 JSON 对象。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any


def immutable_object_digest(
    *,
    object_type: str,
    schema_version: int,
    content: dict[str, Any],
) -> str:
    """计算包含类型与版本的规范 JSON SHA-256。"""
    envelope = {
        "object_type": object_type,
        "schema_version": schema_version,
        "content": content,
    }
    encoded = json.dumps(
        envelope,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class ImmutableObject:
    object_id: str
    object_type: str
    schema_version: int
    digest: str
    content: dict[str, Any]
    created_session_id: str
    created_sequence: int
    created_at: datetime
