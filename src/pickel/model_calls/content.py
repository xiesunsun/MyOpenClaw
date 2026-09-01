"""ModelCall RequestContent / ResponseContent 的版本化 codec。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping

from pickel.context.model_context import ModelContext, model_context_from_dict
from pickel.conversations.agent_message import (
    AssistantMessage,
    agent_message_from_dict,
    agent_message_to_dict,
)
from pickel.shared.frozen_json import (
    FrozenJSON,
    freeze_json_object,
    thaw_json,
)

SCHEMA_VERSION = 1


@dataclass(frozen=True)
class RequestContent:
    model_context: ModelContext
    wire_request: Mapping[str, FrozenJSON]
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError(
                f"不支持的 RequestContent schema_version: {self.schema_version}"
            )
        object.__setattr__(self, "wire_request", freeze_json_object(self.wire_request))


@dataclass(frozen=True)
class ResponseContent:
    partial: bool
    provider_response: Mapping[str, FrozenJSON]
    assistant_message: AssistantMessage
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError(
                f"不支持的 ResponseContent schema_version: {self.schema_version}"
            )
        object.__setattr__(
            self,
            "provider_response",
            freeze_json_object(self.provider_response),
        )


def encode_request_content(content: RequestContent) -> bytes:
    return _canonical_json_bytes(
        {
            "schema_version": content.schema_version,
            "model_context": content.model_context.to_dict(),
            "wire_request": thaw_json(content.wire_request),
        }
    )


def decode_request_content(value: bytes) -> RequestContent:
    data = _decode_object(value, "RequestContent")
    _require_exact_keys(data, {"schema_version", "model_context", "wire_request"})
    model_context = data["model_context"]
    wire_request = data["wire_request"]
    if not isinstance(model_context, dict):
        raise TypeError("RequestContent.model_context 必须是 JSON object")
    if not isinstance(wire_request, dict):
        raise TypeError("RequestContent.wire_request 必须是 JSON object")
    return RequestContent(
        schema_version=_integer(data, "schema_version"),
        model_context=model_context_from_dict(model_context),
        wire_request=wire_request,
    )


def encode_response_content(content: ResponseContent) -> bytes:
    return _canonical_json_bytes(
        {
            "schema_version": content.schema_version,
            "partial": content.partial,
            "provider_response": thaw_json(content.provider_response),
            "assistant_message": agent_message_to_dict(content.assistant_message),
        }
    )


def decode_response_content(value: bytes) -> ResponseContent:
    data = _decode_object(value, "ResponseContent")
    _require_exact_keys(
        data,
        {
            "schema_version",
            "partial",
            "provider_response",
            "assistant_message",
        },
    )
    partial = data["partial"]
    provider_response = data["provider_response"]
    assistant_message = data["assistant_message"]
    if not isinstance(partial, bool):
        raise TypeError("ResponseContent.partial 必须是 boolean")
    if not isinstance(provider_response, dict):
        raise TypeError("ResponseContent.provider_response 必须是 JSON object")
    if not isinstance(assistant_message, dict):
        raise TypeError("ResponseContent.assistant_message 必须是 JSON object")
    message = agent_message_from_dict(assistant_message)
    if not isinstance(message, AssistantMessage):
        raise TypeError("ResponseContent.assistant_message 必须是 AssistantMessage")
    return ResponseContent(
        schema_version=_integer(data, "schema_version"),
        partial=partial,
        provider_response=provider_response,
        assistant_message=message,
    )


def _canonical_json_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _decode_object(value: bytes, name: str) -> dict[str, Any]:
    try:
        decoded = value.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{name} 必须是 UTF-8 JSON") from exc
    try:
        data = json.loads(decoded)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{name} 必须是合法 JSON object") from exc
    if not isinstance(data, dict):
        raise TypeError(f"{name} 必须是 JSON object")
    return data


def _require_exact_keys(value: Mapping[str, Any], keys: set[str]) -> None:
    actual = set(value)
    if actual != keys:
        raise ValueError(
            f"内容字段不匹配，缺少={sorted(keys - actual)}，多余={sorted(actual - keys)}"
        )


def _integer(value: Mapping[str, Any], key: str) -> int:
    item = value[key]
    if isinstance(item, bool) or not isinstance(item, int):
        raise TypeError(f"{key} 必须是 integer")
    return item
