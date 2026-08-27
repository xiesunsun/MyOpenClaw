"""Parent Operation 与 child Session 的不可变因果关系。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class AgentDelegation:
    child_session_id: str
    child_package_version_id: str
    parent_operation_id: str
    parent_step_id: str
    parent_tool_call_id: str
    initial_message_id: str
    created_at: datetime

    def __post_init__(self) -> None:
        for name in (
            "child_session_id",
            "child_package_version_id",
            "parent_operation_id",
            "parent_step_id",
            "parent_tool_call_id",
            "initial_message_id",
        ):
            if not getattr(self, name):
                raise ValueError(f"{name} 不能为空")

    def to_dict(self) -> dict[str, Any]:
        return {
            "child_session_id": self.child_session_id,
            "child_package_version_id": self.child_package_version_id,
            "parent_operation_id": self.parent_operation_id,
            "parent_step_id": self.parent_step_id,
            "parent_tool_call_id": self.parent_tool_call_id,
            "initial_message_id": self.initial_message_id,
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

    @classmethod
    def from_json(cls, value: str) -> "AgentDelegation":
        try:
            data = json.loads(value)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("AgentDelegation 必须是合法 JSON object") from exc
        if not isinstance(data, dict):
            raise TypeError("AgentDelegation 必须是 JSON object")
        keys = set(cls.__dataclass_fields__)
        if set(data) != keys:
            raise ValueError(
                f"JSON 字段不匹配，missing={sorted(keys - set(data))}, "
                f"extra={sorted(set(data) - keys)}"
            )
        timestamp = data["created_at"]
        if not isinstance(timestamp, str):
            raise TypeError("created_at 必须是 ISO8601 字符串")
        try:
            created_at = datetime.fromisoformat(timestamp)
        except ValueError as exc:
            raise ValueError("created_at 不是合法 ISO8601 时间") from exc
        fields = {name: _string(data, name) for name in keys - {"created_at"}}
        return cls(**fields, created_at=created_at)


def _string(data: dict[str, Any], key: str) -> str:
    result = data.get(key)
    if not isinstance(result, str):
        raise TypeError(f"{key} 必须是字符串")
    return result
