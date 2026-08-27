"""ModelCall 内容、attempt 与恢复的窄领域服务。"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Callable, Protocol
from uuid import uuid4

from pickel.context.model_context import ModelContext
from pickel.conversations.agent_message import AssistantMessage
from pickel.conversations.conversation_node import ConversationNode
from pickel.model_calls.content import (
    RequestContent,
    ResponseContent,
    decode_request_content,
    encode_request_content,
    encode_response_content,
)
from pickel.model_calls.content_store import ModelCallContentRef
from pickel.model_calls.model_call import ModelCall, ModelCallError
from pickel.model_calls.prepared import PreparedModelCall
from pickel.model_calls.store import ModelCallStore
from pickel.operations.agent_run_state import AgentRunState
from pickel.operations.session_operation import SessionOperation
from pickel.providers.errors import ProviderRequestError
from pickel.shared.execution_identity import ExecutionIdentity


class ModelCallMapper(Protocol):
    def prepare(self, context: ModelContext) -> PreparedModelCall: ...


class ModelCallPrepareConflict(RuntimeError):
    """ModelCall prepared 事务 CAS 失败，Provider 尚未调用。"""


class ModelCallRecoveryError(RuntimeError):
    """持久化 ModelCall 与当前 AgentRunState 无法安全恢复。"""


class ModelCallRetryExhausted(ModelCallRecoveryError):
    """冻结的真实调用次数已经达到策略上限。"""

    def __init__(self, call: ModelCall | None) -> None:
        super().__init__("模型请求已达到最大真实调用次数")
        self.call = call


@dataclass(frozen=True)
class AgentPreparedModelCall:
    state: AgentRunState
    model_call: ModelCall
    prepared: PreparedModelCall
    reused: bool
    retry_after_attempt: int | None = None


@dataclass(frozen=True)
class SessionPreparedModelCall:
    model_call: ModelCall
    prepared: PreparedModelCall


@dataclass(frozen=True)
class ModelCallResponse:
    assistant_message: AssistantMessage
    provider_response: dict
    started_at: datetime
    first_chunk_at: datetime | None
    finished_at: datetime
    http_status: int | None = None


class ModelCallService:
    """只处理可靠 ModelCall 事实，不调用 Provider。"""

    def __init__(
        self,
        store: ModelCallStore,
        *,
        model_call_id_factory: Callable[[], str] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._content_store = store.model_call_content_store
        self._model_call_id = model_call_id_factory or (lambda: str(uuid4()))
        self._now = now or (lambda: datetime.now(timezone.utc))

    @property
    def store(self) -> ModelCallStore:
        return self._store

    def prepare_or_recover_agent_call(
        self,
        *,
        operation: SessionOperation,
        state: AgentRunState,
        mapper: ModelCallMapper,
        max_attempts: int,
    ) -> AgentPreparedModelCall:
        step = state.current_step
        if (
            state.status != "running"
            or step is None
            or step.phase != "request_ready"
            or step.request_intent is None
        ):
            raise ModelCallRecoveryError(
                "只有 running/request_ready AgentRunState 可以准备模型调用"
            )
        if max_attempts < 1:
            raise ValueError("max_attempts 必须大于 0")

        calls = self._store.list_model_calls(
            session_id=operation.session_id,
            operation_id=operation.operation_id,
            step_id=step.step_id,
        )
        latest = calls[-1] if calls else None
        if latest is not None:
            if latest.request_attempt > step.request_attempt:
                raise ModelCallRecoveryError("ModelCall attempt 超前于 AgentRunState")
            if latest.request_attempt < step.request_attempt:
                raise ModelCallRecoveryError("AgentRunState attempt 缺少对应 ModelCall")
            if latest.status == "prepared":
                return AgentPreparedModelCall(
                    state=state,
                    model_call=latest,
                    prepared=self._load_prepared(
                        step.request_intent.model_context, latest
                    ),
                    reused=True,
                    retry_after_attempt=None,
                )
            if latest.status == "in_flight":
                latest = self.mark_incomplete(latest)
            elif latest.status == "completed":
                raise ModelCallRecoveryError(
                    "request_ready Step 不能对应已 completed 的 ModelCall"
                )
            elif latest.status == "failed":
                if latest.error is None or not latest.error.retryable:
                    raise ModelCallRetryExhausted(latest)
            elif latest.status not in {"cancelled", "incomplete"}:
                raise ModelCallRecoveryError(f"未知 ModelCall status: {latest.status}")

        if step.request_attempt >= max_attempts:
            raise ModelCallRetryExhausted(latest)
        return self._prepare_new_agent_call(
            operation=operation,
            state=state,
            mapper=mapper,
            retry_after_attempt=(
                latest.request_attempt if latest is not None else None
            ),
        )

    def prepare_session_call(
        self,
        *,
        session_id: str,
        context: ModelContext,
        mapper: ModelCallMapper,
        request_attempt: int,
        model_role: str,
        purpose: str,
    ) -> SessionPreparedModelCall:
        prepared = mapper.prepare(context)
        ref = self._put_request_content(context, prepared)
        timestamp = self._now()
        call = ModelCall(
            model_call_id=self._model_call_id(),
            identity=ExecutionIdentity(session_id=session_id),
            request_attempt=request_attempt,
            model_role=model_role,
            purpose=purpose,
            provider=prepared.provider,
            api_kind=prepared.api_kind,
            endpoint=prepared.endpoint,
            requested_model=prepared.requested_model,
            returned_model=None,
            status="prepared",
            request_content_ref=ref.to_string(),
            response_content_ref=None,
            context_fingerprint=None,
            provider_request_id=None,
            http_status=None,
            error=None,
            created_at=timestamp,
            started_at=None,
            first_chunk_at=None,
            finished_at=None,
        )
        self._store.insert_session_model_call(model_call=call)
        return SessionPreparedModelCall(model_call=call, prepared=prepared)

    def load_prepared(self, call: ModelCall) -> PreparedModelCall:
        return self._load_prepared(None, call)

    def mark_failed(
        self,
        call: ModelCall,
        error: ProviderRequestError,
        *,
        first_chunk_at: datetime | None = None,
        response_content_ref: str | None = None,
    ) -> ModelCall:
        failed = replace(
            call,
            status="failed",
            response_content_ref=response_content_ref,
            error=ModelCallError(
                code=error.code,
                message=str(error),
                retryable=error.retryable,
            ),
            http_status=error.status_code,
            first_chunk_at=first_chunk_at,
            finished_at=self._now(),
        )
        if not self._store.transition_model_call(
            model_call=failed,
            expected_status="in_flight",
        ):
            raise ModelCallRecoveryError("ModelCall failed CAS 失败")
        return failed

    def mark_incomplete(
        self,
        call: ModelCall,
        *,
        first_chunk_at: datetime | None = None,
    ) -> ModelCall:
        incomplete = replace(
            call,
            status="incomplete",
            first_chunk_at=first_chunk_at or call.first_chunk_at,
            finished_at=self._now(),
        )
        if not self._store.transition_model_call(
            model_call=incomplete,
            expected_status="in_flight",
        ):
            raise ModelCallRecoveryError("ModelCall incomplete CAS 失败")
        return incomplete

    def mark_cancelled(
        self,
        call: ModelCall,
        *,
        first_chunk_at: datetime | None = None,
    ) -> ModelCall:
        cancelled = replace(
            call,
            status="cancelled",
            first_chunk_at=first_chunk_at or call.first_chunk_at,
            finished_at=self._now(),
        )
        if not self._store.transition_model_call(
            model_call=cancelled,
            expected_status=call.status,
        ):
            raise ModelCallRecoveryError("ModelCall cancelled CAS 失败")
        return cancelled

    def commit_agent_response(
        self,
        *,
        call: ModelCall,
        response: ModelCallResponse,
        state: AgentRunState,
        expected_revision: int,
        node: ConversationNode,
        response_content_ref: str | None = None,
    ) -> ModelCall:
        response_ref = (
            ModelCallContentRef.from_string(response_content_ref)
            if response_content_ref is not None
            else ModelCallContentRef.from_string(self.save_response_content(response))
        )
        completed = self._completed_call(call, response, response_ref)
        if not self._store.commit_agent_model_response(
            model_call=completed,
            state=state,
            expected_revision=expected_revision,
            node=node,
            updated_at=response.finished_at,
        ):
            raise ModelCallPrepareConflict(
                "ResponseContent 已保存，但 ModelCall/Assistant/State 原子 CAS 失败"
            )
        return completed

    def save_response_content(
        self, response: ModelCallResponse, *, partial: bool = False
    ) -> str:
        """先保存 Provider 聚合响应，返回可挂到 ModelCall 的内容引用。"""
        ref = self._content_store.put(
            encode_response_content(
                ResponseContent(
                    partial=partial,
                    provider_response=response.provider_response,
                    assistant_message=response.assistant_message,
                )
            )
        )
        return ref.to_string()

    def commit_agent_processing_failure(
        self,
        *,
        call: ModelCall,
        response: ModelCallResponse,
        state: AgentRunState,
        expected_revision: int,
        node: ConversationNode,
        error: ProviderRequestError,
        response_content_ref: str | None = None,
    ) -> ModelCall:
        """Provider 已成功返回，但响应处理失败时原子保存并收敛。"""
        del error
        response_ref = (
            ModelCallContentRef.from_string(response_content_ref)
            if response_content_ref is not None
            else ModelCallContentRef.from_string(self.save_response_content(response))
        )
        completed = self._completed_call(call, response, response_ref)
        if not self._store.commit_agent_model_processing_failure(
            model_call=completed,
            state=state,
            expected_revision=expected_revision,
            node=node,
            updated_at=response.finished_at,
        ):
            raise ModelCallPrepareConflict(
                "ResponseContent 已保存，但 completed ModelCall/Assistant/failed State 原子 CAS 失败"
            )
        return completed

    def complete_session_response(
        self,
        *,
        call: ModelCall,
        response: ModelCallResponse,
    ) -> ModelCall:
        response_ref = self._content_store.put(
            encode_response_content(
                ResponseContent(
                    partial=False,
                    provider_response=response.provider_response,
                    assistant_message=response.assistant_message,
                )
            )
        )
        completed = self._completed_call(call, response, response_ref)
        if not self._store.transition_model_call(
            model_call=completed,
            expected_status="in_flight",
        ):
            raise ModelCallPrepareConflict("Session ModelCall completed CAS 失败")
        return completed

    def request_content(self, call: ModelCall) -> RequestContent:
        ref = ModelCallContentRef.from_string(call.request_content_ref)
        return decode_request_content(self._content_store.get(ref))

    def _prepare_new_agent_call(
        self,
        *,
        operation: SessionOperation,
        state: AgentRunState,
        mapper: ModelCallMapper,
        retry_after_attempt: int | None,
    ) -> AgentPreparedModelCall:
        step = state.current_step
        assert step is not None and step.request_intent is not None
        prepared = mapper.prepare(step.request_intent.model_context)
        ref = self._put_request_content(step.request_intent.model_context, prepared)
        next_step = replace(step, request_attempt=step.request_attempt + 1)
        next_state = replace(
            state,
            revision=state.revision + 1,
            current_step=next_step,
        )
        timestamp = self._now()
        call = ModelCall(
            model_call_id=self._model_call_id(),
            identity=ExecutionIdentity(
                session_id=operation.session_id,
                operation_id=operation.operation_id,
                step_id=next_step.step_id,
                step_sequence=next_step.step_sequence,
            ),
            request_attempt=next_step.request_attempt,
            model_role="primary",
            purpose="agent_step",
            provider=prepared.provider,
            api_kind=prepared.api_kind,
            endpoint=prepared.endpoint,
            requested_model=prepared.requested_model,
            returned_model=None,
            status="prepared",
            request_content_ref=ref.to_string(),
            response_content_ref=None,
            context_fingerprint=next_step.request_intent.context_fingerprint,
            provider_request_id=None,
            http_status=None,
            error=None,
            created_at=timestamp,
            started_at=None,
            first_chunk_at=None,
            finished_at=None,
        )
        if not self._store.prepare_agent_model_call(
            model_call=call,
            state=next_state,
            expected_revision=state.revision,
            updated_at=timestamp,
        ):
            raise ModelCallPrepareConflict(
                "ModelCall prepared 事务 CAS 失败，Provider 未调用"
            )
        return AgentPreparedModelCall(
            state=next_state,
            model_call=call,
            prepared=prepared,
            reused=False,
            retry_after_attempt=retry_after_attempt,
        )

    def _load_prepared(
        self,
        expected_context: ModelContext | None,
        call: ModelCall,
    ) -> PreparedModelCall:
        content = self.request_content(call)
        if expected_context is not None and content.model_context != expected_context:
            raise ModelCallRecoveryError("RequestContent ModelContext 与 Intent 不一致")
        prepared = PreparedModelCall(
            provider=call.provider,
            api_kind=call.api_kind,
            endpoint=call.endpoint,
            requested_model=call.requested_model,
            body=content.wire_request,
        )
        return prepared

    def _put_request_content(
        self,
        context: ModelContext,
        prepared: PreparedModelCall,
    ) -> ModelCallContentRef:
        return self._content_store.put(
            encode_request_content(
                RequestContent(
                    model_context=context,
                    wire_request=prepared.body,
                )
            )
        )

    @staticmethod
    def _completed_call(
        call: ModelCall,
        response: ModelCallResponse,
        response_ref: ModelCallContentRef,
    ) -> ModelCall:
        metadata = response.assistant_message.metadata
        returned_model = (
            metadata.provider_model_version if metadata is not None else None
        )
        provider_request_id = (
            metadata.provider_response_id if metadata is not None else None
        )
        return replace(
            call,
            returned_model=returned_model,
            status="completed",
            response_content_ref=response_ref.to_string(),
            provider_request_id=provider_request_id,
            http_status=response.http_status,
            error=None,
            started_at=response.started_at,
            first_chunk_at=response.first_chunk_at,
            finished_at=response.finished_at,
        )
