"""Pickel 第一批宿主调用的数据合同。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from pickel.runs.host_calls import HostCallSpec


@dataclass(frozen=True)
class HostCallSource:
    kind: Literal["mcp", "tool", "runtime"]
    name: str
    operation: str | None = None


@dataclass(frozen=True)
class ConfirmationRequest:
    source: HostCallSource
    title: str
    message: str


@dataclass(frozen=True)
class ConfirmationAnswer:
    decision: Literal["accept", "decline"]


@dataclass(frozen=True)
class StructuredInputRequest:
    source: HostCallSource
    title: str
    message: str
    schema: dict[str, Any]


@dataclass(frozen=True)
class StructuredInputAnswer:
    action: Literal["accept", "decline", "cancel"]
    content: dict[str, Any] | None = None


@dataclass(frozen=True)
class ExternalActionRequest:
    source: HostCallSource
    title: str
    message: str
    url: str


@dataclass(frozen=True)
class ExternalActionAnswer:
    action: Literal["accept", "decline", "cancel"]


CONFIRMATION_CALL = HostCallSpec(
    name="host.confirmation",
    version=1,
    request_type=ConfirmationRequest,
    response_type=ConfirmationAnswer,
)

STRUCTURED_INPUT_CALL = HostCallSpec(
    name="host.structured_input",
    version=1,
    request_type=StructuredInputRequest,
    response_type=StructuredInputAnswer,
)

EXTERNAL_ACTION_CALL = HostCallSpec(
    name="host.external_action",
    version=1,
    request_type=ExternalActionRequest,
    response_type=ExternalActionAnswer,
)
