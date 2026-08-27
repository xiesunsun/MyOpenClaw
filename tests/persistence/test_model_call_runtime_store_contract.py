"""SQLite/InMemory 的 ModelCall prepared 事务共享合同。"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from pickel.agents.agent_package import (
    AgentRuntimePolicy,
    ImplementationRef,
    ModelPolicy,
    ModelVersion,
    WorkspacePolicy,
    build_agent_package_version,
)
from pickel.context.model_context import ModelContext, SystemContent
from pickel.conversations.agent_message import UserMessage
from pickel.conversations.content_blocks import TextBlock
from pickel.conversations.conversation_session import ConversationSession
from pickel.inbox.message import UserMessageSource
from pickel.model_calls.content import RequestContent, encode_request_content
from pickel.model_calls.content import decode_response_content
from pickel.model_calls.content_store import (
    ModelCallContentRef,
    ModelCallContentStore,
)
from pickel.model_calls.model_call import ModelCall
from pickel.operations.agent_run_state import (
    AgentRunError,
    AgentRunState,
    ModelRequestIntent,
    ModelStepState,
)
from pickel.operations.session_operation import SessionOperation
from pickel.persistence.errors import StorageConflictError, StorageIntegrityError
from pickel.persistence.in_memory_runtime_store import InMemoryRuntimeStore
from pickel.persistence.sqlite_runtime_store import SQLiteRuntimeStore
from pickel.shared.execution_identity import ExecutionIdentity
from pickel.workspaces.workspace import Workspace
from pickel.workspaces.workspace_binding import WorkspaceBinding

UTC = timezone.utc
NOW = datetime(2026, 8, 27, tzinfo=UTC)


@pytest.fixture(params=("memory", "sqlite"))
def store_and_content(
    request: pytest.FixtureRequest,
    tmp_path: Path,
) -> tuple[Any, ModelCallContentStore]:
    if request.param == "memory":
        store = InMemoryRuntimeStore()
    else:
        store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    return store, store.model_call_content_store


def _package():
    return build_agent_package_version(
        agent_id="agent-1",
        format_version=1,
        behavior_instruction="test",
        model_policy=ModelPolicy(
            primary=ModelVersion(
                provider="test",
                model="test-model",
                wire_protocol="test",
                api_base=None,
                temperature=None,
                max_input_tokens=None,
                max_output_tokens=128,
                provider_options={},
                provider_implementation=ImplementationRef("provider", "test"),
                required_secret_refs=(),
            )
        ),
        runtime_policy=AgentRuntimePolicy(3, 10),
        workspace_policy=WorkspacePolicy("workspace"),
        skills=(),
        tools=(),
        extensions=(),
        created_at=NOW,
    )


def _setup_request_ready(store: Any, root: Path) -> AgentRunState:
    root.mkdir(parents=True, exist_ok=True)
    store.create_session(
        workspace=Workspace("workspace-1", root, NOW),
        session=ConversationSession(
            session_id="session-1",
            agent_id="agent-1",
            workspace_id="workspace-1",
            cwd=root,
            active_node_id=None,
            active_operation_id=None,
            title=None,
            title_source=None,
            created_at=NOW,
            updated_at=NOW,
            archived_at=None,
        ),
    )
    package = _package()
    store.insert_agent_package_version(package)
    store.send_message(
        message_id="message-1",
        session_id="session-1",
        delivery="followup",
        message=UserMessage((TextBlock("hello"),)),
        source=UserMessageSource(),
        created_at=NOW,
    )
    operation = SessionOperation(
        operation_id="operation-1",
        session_id="session-1",
        agent_package_version_id=package.package_version_id,
        workspace_binding=WorkspaceBinding("workspace-1", root, None),
        input_node_id="message-1",
        accepted_at=NOW,
    )
    queued = AgentRunState(
        operation_id=operation.operation_id,
        revision=1,
        status="queued",
        waiting_reason=None,
        completed_step_count=0,
        current_step=None,
        final_assistant_node_id=None,
        error=None,
        cancellation=None,
    )
    assert store.accept_operation(
        operation=operation,
        state=queued,
        expected_node_id=None,
    )
    context = ModelContext(SystemContent.from_text("system"), (), ())
    step = ModelStepState(
        step_id="step-1",
        step_sequence=1,
        phase="request_ready",
        request_attempt=0,
        request_intent=ModelRequestIntent(
            model_context=context,
            context_fingerprint="fingerprint-1",
        ),
        assistant_message_node_id=None,
        tool_calls=(),
    )
    ready = AgentRunState(
        operation_id=operation.operation_id,
        revision=2,
        status="running",
        waiting_reason=None,
        completed_step_count=0,
        current_step=step,
        final_assistant_node_id=None,
        error=None,
        cancellation=None,
    )
    assert store.commit_run_transition(
        state=ready,
        expected_revision=1,
        node=None,
        updated_at=NOW,
    )
    return ready


def _save_request(
    content_store: ModelCallContentStore,
    state: AgentRunState,
) -> ModelCallContentRef:
    step = state.current_step
    assert step is not None and step.request_intent is not None
    return content_store.put(
        encode_request_content(
            RequestContent(
                model_context=step.request_intent.model_context,
                wire_request={"model": "test-model", "stream": True},
            )
        )
    )


def _prepared(
    *,
    ref: ModelCallContentRef,
    request_attempt: int,
    model_call_id: str = "call-1",
) -> ModelCall:
    return ModelCall(
        model_call_id=model_call_id,
        identity=ExecutionIdentity(
            session_id="session-1",
            operation_id="operation-1",
            step_id="step-1",
            step_sequence=1,
        ),
        request_attempt=request_attempt,
        model_role="primary",
        purpose="agent_step",
        provider="test",
        api_kind="test",
        endpoint="/generate",
        requested_model="test-model",
        returned_model=None,
        status="prepared",
        request_content_ref=ref.to_string(),
        response_content_ref=None,
        context_fingerprint="fingerprint-1",
        provider_request_id=None,
        http_status=None,
        error=None,
        created_at=NOW,
        started_at=None,
        first_chunk_at=None,
        finished_at=None,
    )


def _next_attempt(state: AgentRunState) -> AgentRunState:
    assert state.current_step is not None
    return replace(
        state,
        revision=state.revision + 1,
        current_step=replace(
            state.current_step,
            request_attempt=state.current_step.request_attempt + 1,
        ),
    )


def test_prepare_agent_model_call_atomically_advances_attempt_and_inserts_call(
    store_and_content: tuple[Any, ModelCallContentStore],
    tmp_path: Path,
) -> None:
    store, content_store = store_and_content
    ready = _setup_request_ready(store, tmp_path / "workspace")
    ref = _save_request(content_store, ready)
    next_state = _next_attempt(ready)

    assert store.prepare_agent_model_call(
        model_call=_prepared(ref=ref, request_attempt=1),
        state=next_state,
        expected_revision=ready.revision,
        updated_at=NOW,
    )

    persisted = store.load_model_call("call-1")
    assert persisted is not None
    assert persisted.status == "prepared"
    assert persisted.request_content_ref == ref.to_string()
    state = store.load_run_state("operation-1")
    assert state is not None and state.current_step is not None
    assert state.revision == 3
    assert state.current_step.request_attempt == 1


def test_missing_request_content_never_advances_attempt_or_inserts_call(
    store_and_content: tuple[Any, ModelCallContentStore],
    tmp_path: Path,
) -> None:
    store, content_store = store_and_content
    ready = _setup_request_ready(store, tmp_path / "workspace")
    ref = _save_request(content_store, ready)
    content_store.delete(ref)

    with pytest.raises(StorageIntegrityError):
        store.prepare_agent_model_call(
            model_call=_prepared(ref=ref, request_attempt=1),
            state=_next_attempt(ready),
            expected_revision=ready.revision,
            updated_at=NOW,
        )

    assert store.load_model_call("call-1") is None
    persisted = store.load_run_state("operation-1")
    assert persisted is not None and persisted.current_step is not None
    assert persisted.revision == ready.revision
    assert persisted.current_step.request_attempt == 0


def test_stale_state_cas_does_not_create_second_model_call(
    store_and_content: tuple[Any, ModelCallContentStore],
    tmp_path: Path,
) -> None:
    store, content_store = store_and_content
    ready = _setup_request_ready(store, tmp_path / "workspace")
    ref = _save_request(content_store, ready)
    next_state = _next_attempt(ready)
    assert store.prepare_agent_model_call(
        model_call=_prepared(ref=ref, request_attempt=1),
        state=next_state,
        expected_revision=ready.revision,
        updated_at=NOW,
    )

    assert not store.prepare_agent_model_call(
        model_call=_prepared(
            ref=ref,
            request_attempt=1,
            model_call_id="call-2",
        ),
        state=next_state,
        expected_revision=ready.revision,
        updated_at=NOW,
    )
    assert [
        call.model_call_id
        for call in store.list_model_calls(
            session_id="session-1",
            operation_id="operation-1",
            step_id="step-1",
        )
    ] == ["call-1"]


def test_model_call_insert_failure_rolls_back_attempt_increment(
    store_and_content: tuple[Any, ModelCallContentStore],
    tmp_path: Path,
) -> None:
    store, content_store = store_and_content
    ready = _setup_request_ready(store, tmp_path / "workspace")
    ref = _save_request(content_store, ready)
    attempt_one = _next_attempt(ready)
    assert store.prepare_agent_model_call(
        model_call=_prepared(ref=ref, request_attempt=1),
        state=attempt_one,
        expected_revision=ready.revision,
        updated_at=NOW,
    )

    attempt_two = _next_attempt(attempt_one)
    with pytest.raises(StorageConflictError):
        store.prepare_agent_model_call(
            model_call=_prepared(ref=ref, request_attempt=2),
            state=attempt_two,
            expected_revision=attempt_one.revision,
            updated_at=NOW,
        )

    persisted = store.load_run_state("operation-1")
    assert persisted is not None and persisted.current_step is not None
    assert persisted.revision == attempt_one.revision
    assert persisted.current_step.request_attempt == 1


def test_committed_model_call_with_missing_content_fails_loudly(
    store_and_content: tuple[Any, ModelCallContentStore],
    tmp_path: Path,
) -> None:
    store, content_store = store_and_content
    ready = _setup_request_ready(store, tmp_path / "workspace")
    ref = _save_request(content_store, ready)
    assert store.prepare_agent_model_call(
        model_call=_prepared(ref=ref, request_attempt=1),
        state=_next_attempt(ready),
        expected_revision=ready.revision,
        updated_at=NOW,
    )
    content_store.delete(ref)

    with pytest.raises(StorageIntegrityError):
        store.load_model_call("call-1")


def test_response_content_and_assistant_commit_are_atomic(
    store_and_content: tuple[Any, ModelCallContentStore],
    tmp_path: Path,
) -> None:
    from pickel.conversations.agent_message import AssistantMessage
    from pickel.conversations.conversation_node import ConversationNode
    from pickel.model_calls.service import ModelCallResponse, ModelCallService

    store, content_store = store_and_content
    ready = _setup_request_ready(store, tmp_path / "workspace")
    ref = _save_request(content_store, ready)
    prepared = _prepared(ref=ref, request_attempt=1)
    sent_state = _next_attempt(ready)
    assert store.prepare_agent_model_call(
        model_call=prepared,
        state=sent_state,
        expected_revision=ready.revision,
        updated_at=NOW,
    )
    in_flight = replace(prepared, status="in_flight", started_at=NOW)
    assert store.transition_model_call(
        model_call=in_flight,
        expected_status="prepared",
    )

    message = AssistantMessage((TextBlock("done"),))
    assert sent_state.current_step is not None
    next_step = replace(
        sent_state.current_step,
        phase="awaiting_tools",
        request_intent=None,
        assistant_message_node_id="assistant-1",
        tool_calls=(),
    )
    next_state = replace(
        sent_state,
        revision=sent_state.revision + 1,
        current_step=next_step,
    )
    node = ConversationNode(
        node_id="assistant-1",
        session_id="session-1",
        parent_node_id="message-1",
        content_type="agent_message",
        content=message,
        created_at=NOW,
    )
    response = ModelCallResponse(
        assistant_message=message,
        provider_response={"id": "resp-1"},
        started_at=NOW,
        first_chunk_at=NOW,
        finished_at=NOW,
        http_status=200,
    )

    completed = ModelCallService(store).commit_agent_response(
        call=in_flight,
        response=response,
        state=next_state,
        expected_revision=sent_state.revision,
        node=node,
    )

    assert completed.status == "completed"
    persisted = store.load_model_call("call-1")
    assert persisted is not None and persisted.status == "completed"
    assert store.load_session("session-1").active_node_id == "assistant-1"
    assert store.load_node("assistant-1").content == message


def test_stale_response_cas_leaves_call_in_flight_and_no_assistant_node(
    store_and_content: tuple[Any, ModelCallContentStore],
    tmp_path: Path,
) -> None:
    from pickel.conversations.agent_message import AssistantMessage
    from pickel.conversations.conversation_node import ConversationNode
    from pickel.model_calls.service import (
        ModelCallPrepareConflict,
        ModelCallResponse,
        ModelCallService,
    )

    store, content_store = store_and_content
    ready = _setup_request_ready(store, tmp_path / "workspace")
    ref = _save_request(content_store, ready)
    prepared = _prepared(ref=ref, request_attempt=1)
    sent_state = _next_attempt(ready)
    assert store.prepare_agent_model_call(
        model_call=prepared,
        state=sent_state,
        expected_revision=ready.revision,
        updated_at=NOW,
    )
    in_flight = replace(prepared, status="in_flight", started_at=NOW)
    assert store.transition_model_call(
        model_call=in_flight,
        expected_status="prepared",
    )
    assert sent_state.current_step is not None
    message = AssistantMessage((TextBlock("done"),))
    next_state = replace(
        sent_state,
        revision=sent_state.revision + 1,
        current_step=replace(
            sent_state.current_step,
            phase="awaiting_tools",
            request_intent=None,
            assistant_message_node_id="assistant-stale",
            tool_calls=(),
        ),
    )
    node = ConversationNode(
        node_id="assistant-stale",
        session_id="session-1",
        parent_node_id="message-1",
        content_type="agent_message",
        content=message,
        created_at=NOW,
    )
    response = ModelCallResponse(
        assistant_message=message,
        provider_response={"id": "resp-stale"},
        started_at=NOW,
        first_chunk_at=NOW,
        finished_at=NOW,
        http_status=200,
    )

    with pytest.raises(ModelCallPrepareConflict):
        ModelCallService(store).commit_agent_response(
            call=in_flight,
            response=response,
            state=next_state,
            expected_revision=sent_state.revision - 1,
            node=node,
        )

    persisted = store.load_model_call("call-1")
    assert persisted is not None and persisted.status == "in_flight"
    assert store.load_node("assistant-stale") is None


def test_processing_failure_commits_completed_call_node_and_failed_state(
    store_and_content: tuple[Any, ModelCallContentStore],
    tmp_path: Path,
) -> None:
    from pickel.conversations.agent_message import AssistantMessage
    from pickel.conversations.conversation_node import ConversationNode
    from pickel.model_calls.service import ModelCallResponse, ModelCallService
    from pickel.providers.errors import ProviderRequestError

    store, content_store = store_and_content
    ready = _setup_request_ready(store, tmp_path / "workspace")
    request_ref = _save_request(content_store, ready)
    prepared = _prepared(ref=request_ref, request_attempt=1)
    sent_state = _next_attempt(ready)
    assert store.prepare_agent_model_call(
        model_call=prepared,
        state=sent_state,
        expected_revision=ready.revision,
        updated_at=NOW,
    )
    in_flight = replace(prepared, status="in_flight", started_at=NOW)
    assert store.transition_model_call(
        model_call=in_flight,
        expected_status="prepared",
    )

    message = AssistantMessage((TextBlock("provider result"),))
    node = ConversationNode(
        node_id="assistant-processing-failure",
        session_id="session-1",
        parent_node_id="message-1",
        content_type="agent_message",
        content=message,
        created_at=NOW,
    )
    failed_state = replace(
        sent_state,
        revision=sent_state.revision + 1,
        status="failed",
        current_step=None,
        final_assistant_node_id=None,
        error=AgentRunError(
            code="model_response_processing_failed",
            message="Hook failed",
            retryable=False,
        ),
    )
    response = ModelCallResponse(
        assistant_message=message,
        provider_response={"id": "resp-processing-failure"},
        started_at=NOW,
        first_chunk_at=NOW,
        finished_at=NOW,
        http_status=200,
    )

    completed = ModelCallService(store).commit_agent_processing_failure(
        call=in_flight,
        response=response,
        state=failed_state,
        expected_revision=sent_state.revision,
        node=node,
        error=ProviderRequestError(
            code="model_response_processing_failed",
            message="Hook failed",
            retryable=False,
        ),
    )

    assert completed.status == "completed"
    persisted = store.load_model_call("call-1")
    assert persisted is not None and persisted.status == "completed"
    assert persisted.response_content_ref is not None
    content = decode_response_content(
        content_store.get(
            ModelCallContentRef.from_string(persisted.response_content_ref)
        )
    )
    assert content.partial is False
    assert content.assistant_message == message
    assert store.load_node(node.node_id).content == message
    persisted_state = store.load_run_state("operation-1")
    assert persisted_state is not None
    assert persisted_state.status == "failed"
    assert persisted_state.final_assistant_node_id is None
    assert store.load_session("session-1").active_operation_id is None
