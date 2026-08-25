"""SessionOperation 的不可变执行身份。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from pickel.workspaces.workspace_binding import WorkspaceBinding


@dataclass(frozen=True)
class SessionOperation:
    operation_id: str
    session_id: str
    agent_package_version_id: str
    workspace_binding: WorkspaceBinding
    input_node_id: str
    accepted_at: datetime

    def __post_init__(self) -> None:
        for name in (
            "operation_id",
            "session_id",
            "agent_package_version_id",
            "input_node_id",
        ):
            if not getattr(self, name):
                raise ValueError(f"{name} 不能为空")

    def to_dict(self) -> dict[str, Any]:
        binding = self.workspace_binding
        return {
            "operation_id": self.operation_id,
            "session_id": self.session_id,
            "agent_package_version_id": self.agent_package_version_id,
            "workspace_binding": {
                "workspace_id": binding.workspace_id,
                "working_directory": str(binding.working_directory),
                "allowed_root": (
                    str(binding.allowed_root)
                    if binding.allowed_root is not None
                    else None
                ),
            },
            "input_node_id": self.input_node_id,
            "accepted_at": self.accepted_at.isoformat(),
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
    def from_json(cls, value: str) -> "SessionOperation":
        try:
            data = json.loads(value)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("SessionOperation 必须是合法 JSON object") from exc
        if not isinstance(data, dict):
            raise TypeError("SessionOperation 必须是 JSON object")
        _require_keys(
            data,
            {
                "operation_id",
                "session_id",
                "agent_package_version_id",
                "workspace_binding",
                "input_node_id",
                "accepted_at",
            },
        )
        binding_data = data["workspace_binding"]
        if not isinstance(binding_data, dict):
            raise TypeError("workspace_binding 必须是 JSON object")
        _require_keys(
            binding_data, {"workspace_id", "working_directory", "allowed_root"}
        )
        accepted_at = data["accepted_at"]
        if not isinstance(accepted_at, str):
            raise TypeError("accepted_at 必须是 ISO8601 字符串")
        try:
            timestamp = datetime.fromisoformat(accepted_at)
        except ValueError as exc:
            raise ValueError("accepted_at 不是合法 ISO8601 时间") from exc
        return cls(
            operation_id=_string(data, "operation_id"),
            session_id=_string(data, "session_id"),
            agent_package_version_id=_string(data, "agent_package_version_id"),
            workspace_binding=WorkspaceBinding(
                workspace_id=_string(binding_data, "workspace_id"),
                working_directory=Path(_string(binding_data, "working_directory")),
                allowed_root=(
                    Path(binding_data["allowed_root"])
                    if binding_data["allowed_root"] is not None
                    else None
                ),
            ),
            input_node_id=_string(data, "input_node_id"),
            accepted_at=timestamp,
        )


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
