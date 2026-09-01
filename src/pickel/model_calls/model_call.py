"""一次真实 Provider 生成调用的可靠持久化实体。"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from typing import Literal, Mapping

from pickel.shared.execution_identity import ExecutionIdentity
from pickel.shared.frozen_json import FrozenJSON, freeze_json_object

ModelCallStatus = Literal[
    "prepared",
    "in_flight",
    "completed",
    "failed",
    "cancelled",
    "incomplete",
]
ModelRole = Literal["primary", "worker", "utility"]
ModelCallPurpose = Literal["agent_step", "title", "history_compaction"]

_TERMINAL = frozenset({"completed", "failed", "cancelled", "incomplete"})
_ROLES = frozenset({"primary", "worker", "utility"})
_PURPOSES = frozenset(
    {"agent_step", "title", "history_compaction", "goal_verification"}
)
_STATUSES = frozenset({"prepared", "in_flight", *_TERMINAL})


@dataclass(frozen=True)
class ModelCallError:
    """Provider 调用失败的结构化事实。"""

    code: str
    message: str
    retryable: bool | None = None
    details: Mapping[str, FrozenJSON] | None = None

    def __post_init__(self) -> None:
        if not self.code or not self.message:
            raise ValueError("ModelCallError.code 和 message 不能为空")
        if self.details is not None:
            object.__setattr__(self, "details", freeze_json_object(self.details))


@dataclass(frozen=True)
class ModelCall:
    """一次真实 Provider 生成尝试。"""

    model_call_id: str
    identity: ExecutionIdentity
    request_attempt: int
    model_role: ModelRole
    purpose: ModelCallPurpose
    provider: str
    api_kind: str
    endpoint: str
    requested_model: str
    returned_model: str | None
    status: ModelCallStatus
    request_content_ref: str
    response_content_ref: str | None
    context_fingerprint: str | None
    provider_request_id: str | None
    http_status: int | None
    error: ModelCallError | None
    created_at: datetime
    started_at: datetime | None
    first_chunk_at: datetime | None
    finished_at: datetime | None

    def __post_init__(self) -> None:
        if not self.model_call_id:
            raise ValueError("model_call_id 不能为空")
        if not self.identity.session_id:
            raise ValueError("ModelCall.session_id 不能为空")
        if self.identity.model_call_id not in (None, self.model_call_id):
            raise ValueError("ModelCall.identity.model_call_id 与实体身份不一致")
        if self.identity.model_call_id is None:
            object.__setattr__(
                self,
                "identity",
                replace(self.identity, model_call_id=self.model_call_id),
            )
        if self.request_attempt < 1:
            raise ValueError("request_attempt 必须大于 0")
        if self.model_role not in _ROLES:
            raise ValueError(f"不支持的 model_role: {self.model_role!r}")
        if self.purpose not in _PURPOSES:
            raise ValueError(f"不支持的 purpose: {self.purpose!r}")
        if self.status not in _STATUSES:
            raise ValueError(f"不支持的 ModelCallStatus: {self.status!r}")
        for name, value in (
            ("provider", self.provider),
            ("api_kind", self.api_kind),
            ("endpoint", self.endpoint),
            ("requested_model", self.requested_model),
            ("request_content_ref", self.request_content_ref),
        ):
            if not value:
                raise ValueError(f"{name} 不能为空")
        if self.http_status is not None and not 100 <= self.http_status <= 599:
            raise ValueError("http_status 必须是合法 HTTP 状态码")
        self._validate_identity()
        self._validate_status_fields()

    @property
    def session_id(self) -> str:
        return self.identity.session_id

    @property
    def operation_id(self) -> str | None:
        return self.identity.operation_id

    @property
    def step_id(self) -> str | None:
        return self.identity.step_id

    @property
    def step_sequence(self) -> int | None:
        return self.identity.step_sequence

    @property
    def terminal(self) -> bool:
        return self.status in _TERMINAL

    def _validate_identity(self) -> None:
        identity = self.identity
        if identity.tool_call_id is not None or identity.message_id is not None:
            raise ValueError(
                "ModelCall ExecutionIdentity 不能携带 tool_call_id/message_id"
            )
        if self.purpose == "agent_step":
            if (
                not identity.operation_id
                or not identity.step_id
                or identity.step_sequence is None
                or identity.step_sequence < 1
                or not self.context_fingerprint
            ):
                raise ValueError(
                    "agent_step ModelCall 必须有 operation/step/sequence/fingerprint"
                )
            if self.model_role != "primary":
                raise ValueError("agent_step ModelCall 的 model_role 必须是 primary")
            return
        if any(
            value is not None
            for value in (
                identity.operation_id,
                identity.step_id,
                identity.step_sequence,
            )
        ):
            raise ValueError("Session 级 ModelCall 不能伪造 Operation/Step")
        if self.context_fingerprint is not None:
            raise ValueError("Session 级 ModelCall 不保存 context_fingerprint")
        expected_role = "utility" if self.purpose == "title" else "worker"
        if self.model_role != expected_role:
            raise ValueError(
                f"{self.purpose} ModelCall 的 model_role 必须是 {expected_role}"
            )

    def _validate_status_fields(self) -> None:
        if self.first_chunk_at is not None and self.started_at is None:
            raise ValueError("first_chunk_at 存在时必须有 started_at")
        if (
            self.first_chunk_at is not None
            and self.started_at is not None
            and self.first_chunk_at < self.started_at
        ):
            raise ValueError("first_chunk_at 不能早于 started_at")
        if (
            self.finished_at is not None
            and self.started_at is not None
            and self.finished_at < self.started_at
        ):
            raise ValueError("finished_at 不能早于 started_at")

        if self.status == "prepared":
            if any(
                value is not None
                for value in (
                    self.started_at,
                    self.first_chunk_at,
                    self.finished_at,
                    self.response_content_ref,
                    self.error,
                )
            ):
                raise ValueError("prepared ModelCall 不能有启动、结束、响应或错误字段")
            return
        if self.status == "in_flight":
            if self.started_at is None:
                raise ValueError("in_flight ModelCall 必须有 started_at")
            if any(
                value is not None
                for value in (
                    self.finished_at,
                    self.response_content_ref,
                    self.error,
                )
            ):
                raise ValueError("in_flight ModelCall 不能有结束、响应或错误字段")
            return
        if self.finished_at is None:
            raise ValueError(f"{self.status} ModelCall 必须有 finished_at")
        if self.status == "completed":
            if self.started_at is None:
                raise ValueError("completed ModelCall 必须有 started_at")
            if self.response_content_ref is None or self.error is not None:
                raise ValueError("completed ModelCall 必须有响应且不能有 error")
        elif self.status == "failed":
            if self.started_at is None or self.error is None:
                raise ValueError("failed ModelCall 必须有 started_at 和 error")
        elif self.status in {"cancelled", "incomplete"} and self.error is not None:
            raise ValueError("cancelled/incomplete ModelCall 不使用 error 表达终止原因")
