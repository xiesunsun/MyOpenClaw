"""InboxMessage 及其严格 JSON 表示。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from pickel.conversations.agent_message import (
    UserMessage,
    agent_message_from_dict,
    agent_message_to_dict,
)

MessageDelivery = Literal["followup", "steer", "inject"]
MessageStatus = Literal["pending", "claimed", "discarded"]


@dataclass(frozen=True)
class UserMessageSource:
    kind: Literal["user"] = "user"


@dataclass(frozen=True)
class AgentMessageSource:
    sender_session_id: str
    sender_operation_id: str
    form: Literal["followup", "steer", "inject"]
    kind: Literal["agent"] = "agent"


@dataclass(frozen=True)
class HookMessageSource:
    hook_id: str
    kind: Literal["hook"] = "hook"


@dataclass(frozen=True)
class HostMessageSource:
    call_id: str
    kind: Literal["host"] = "host"


@dataclass(frozen=True)
class RuntimeMessageSource:
    reason: str
    kind: Literal["runtime"] = "runtime"


MessageSource = (
    UserMessageSource
    | AgentMessageSource
    | HookMessageSource
    | HostMessageSource
    | RuntimeMessageSource
)


@dataclass(frozen=True)
class InboxMessage:
    message_id: str
    session_id: str
    sequence: int
    delivery: MessageDelivery
    message: UserMessage
    source: MessageSource
    created_at: datetime
    status: MessageStatus = "pending"
    claimed_operation_id: str | None = None
    claimed_step_id: str | None = None
    outcome_reason: str | None = None
    handled_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.message_id or not self.session_id:
            raise ValueError("message_id 和 session_id 不能为空")
        if self.sequence < 0:
            raise ValueError("sequence 不能小于 0")
        if self.delivery not in ("followup", "steer", "inject"):
            raise ValueError(f"不支持的 delivery: {self.delivery!r}")
        if self.status not in ("pending", "claimed", "discarded"):
            raise ValueError(f"不支持的 status: {self.status!r}")
        if not isinstance(self.message, UserMessage):
            raise TypeError("InboxMessage.message 必须是 UserMessage")
        _validate_source(self.source)
        if self.status == "pending":
            if any(
                value is not None
                for value in (
                    self.claimed_operation_id,
                    self.claimed_step_id,
                    self.outcome_reason,
                    self.handled_at,
                )
            ):
                raise ValueError("pending InboxMessage 不能有处理结果")
        elif self.status == "claimed":
            if not self.claimed_operation_id or self.handled_at is None:
                raise ValueError("claimed InboxMessage 必须有 operation 和 handled_at")
            if self.outcome_reason is not None:
                raise ValueError("claimed InboxMessage 不能有 outcome_reason")
        else:
            if (
                self.claimed_operation_id is not None
                or self.claimed_step_id is not None
                or self.handled_at is None
                or not self.outcome_reason
            ):
                raise ValueError("discarded InboxMessage 的处理字段不完整")

    def to_dict(self) -> dict[str, Any]:
        return {
            "message_id": self.message_id,
            "session_id": self.session_id,
            "sequence": self.sequence,
            "delivery": self.delivery,
            "message": agent_message_to_dict(self.message),
            "source": _source_to_dict(self.source),
            "status": self.status,
            "claimed_operation_id": self.claimed_operation_id,
            "claimed_step_id": self.claimed_step_id,
            "outcome_reason": self.outcome_reason,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "handled_at": self.handled_at.isoformat() if self.handled_at else None,
        }

    def message_payload_dict(self) -> dict[str, Any]:
        """返回数据库 message_json 的内容，不混入 Inbox 行状态。"""
        return {
            "message": agent_message_to_dict(self.message),
            "source": _source_to_dict(self.source),
        }

    def message_payload_json(self) -> str:
        """严格编码 message_json；message_id/status 等字段不在其中。"""
        return json.dumps(
            self.message_payload_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )

    def to_json(self) -> str:
        return json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )

    @classmethod
    def from_json(cls, value: str) -> "InboxMessage":
        try:
            data = json.loads(value)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("InboxMessage 必须是合法 JSON object") from exc
        if not isinstance(data, dict):
            raise TypeError("InboxMessage 必须是 JSON object")
        _require_keys(data, set(cls.__dataclass_fields__))
        return cls(
            message_id=_string(data, "message_id"),
            session_id=_string(data, "session_id"),
            sequence=_integer(data, "sequence"),
            delivery=_literal(data, "delivery", ("followup", "steer", "inject")),
            message=_user_message(data["message"]),
            source=_source_from_dict(data["source"]),
            status=_literal(data, "status", ("pending", "claimed", "discarded")),
            claimed_operation_id=_optional_string(data, "claimed_operation_id"),
            claimed_step_id=_optional_string(data, "claimed_step_id"),
            outcome_reason=_optional_string(data, "outcome_reason"),
            created_at=_datetime(data, "created_at"),
            handled_at=_datetime(data, "handled_at"),
        )


def _validate_source(source: MessageSource) -> None:
    values = [
        isinstance(source, UserMessageSource),
        isinstance(source, AgentMessageSource),
        isinstance(source, HookMessageSource),
        isinstance(source, HostMessageSource),
        isinstance(source, RuntimeMessageSource),
    ]
    if sum(values) != 1:
        raise TypeError("source 必须是已知 MessageSource")
    if isinstance(source, AgentMessageSource):
        if not source.sender_session_id or not source.sender_operation_id:
            raise ValueError("agent source 必须有 sender session 和 operation")
        if source.form not in ("followup", "steer", "inject"):
            raise ValueError("agent source.form 无效")
    for attr in ("hook_id", "call_id", "reason"):
        if hasattr(source, attr) and not getattr(source, attr):
            raise ValueError(f"source.{attr} 不能为空")


def _source_to_dict(source: MessageSource) -> dict[str, Any]:
    if isinstance(source, UserMessageSource):
        return {"kind": "user"}
    if isinstance(source, AgentMessageSource):
        return {
            "kind": "agent",
            "sender_session_id": source.sender_session_id,
            "sender_operation_id": source.sender_operation_id,
            "form": source.form,
        }
    if isinstance(source, HookMessageSource):
        return {"kind": "hook", "hook_id": source.hook_id}
    if isinstance(source, HostMessageSource):
        return {"kind": "host", "call_id": source.call_id}
    if isinstance(source, RuntimeMessageSource):
        return {"kind": "runtime", "reason": source.reason}
    raise TypeError("source 必须是已知 MessageSource")


def _source_from_dict(value: Any) -> MessageSource:
    if not isinstance(value, dict) or not isinstance(value.get("kind"), str):
        raise TypeError("source 必须是带 kind 的 JSON object")
    kind = value["kind"]
    if kind == "user":
        _require_keys(value, {"kind"})
        return UserMessageSource()
    if kind == "agent":
        _require_keys(
            value, {"kind", "sender_session_id", "sender_operation_id", "form"}
        )
        return AgentMessageSource(
            sender_session_id=_string(value, "sender_session_id"),
            sender_operation_id=_string(value, "sender_operation_id"),
            form=_literal(value, "form", ("followup", "steer", "inject")),
        )
    if kind == "hook":
        _require_keys(value, {"kind", "hook_id"})
        return HookMessageSource(_string(value, "hook_id"))
    if kind == "host":
        _require_keys(value, {"kind", "call_id"})
        return HostMessageSource(_string(value, "call_id"))
    if kind == "runtime":
        _require_keys(value, {"kind", "reason"})
        return RuntimeMessageSource(_string(value, "reason"))
    raise ValueError(f"未知 source.kind: {kind!r}")


def _user_message(value: Any) -> UserMessage:
    if not isinstance(value, dict):
        raise TypeError("message 必须是 JSON object")
    result = agent_message_from_dict(value)
    if not isinstance(result, UserMessage):
        raise TypeError("InboxMessage.message 必须是 UserMessage")
    return result


def _require_keys(value: dict[str, Any], keys: set[str]) -> None:
    if set(value) != keys:
        raise ValueError(
            f"JSON 字段不匹配，missing={sorted(keys - set(value))}, "
            f"extra={sorted(set(value) - keys)}"
        )


def _string(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str):
        raise TypeError(f"{key} 必须是字符串")
    return value


def _optional_string(data: dict[str, Any], key: str) -> str | None:
    value = data.get(key)
    if value is not None and not isinstance(value, str):
        raise TypeError(f"{key} 必须是字符串或 null")
    return value


def _integer(data: dict[str, Any], key: str) -> int:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{key} 必须是整数")
    return value


def _literal(data: dict[str, Any], key: str, choices: tuple[str, ...]) -> str:
    value = _string(data, key)
    if value not in choices:
        raise ValueError(f"{key} 值无效: {value!r}")
    return value


def _datetime(data: dict[str, Any], key: str) -> datetime | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"{key} 必须是 ISO8601 字符串或 null")
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{key} 不是合法 ISO8601 时间") from exc
