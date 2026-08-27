from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from pickel.context.model_context import ModelContext, SystemContent
from pickel.conversations.agent_message import (
    AssistantMessage,
    ModelResponseMetadata,
    ModelUsage,
    UserMessage,
)
from pickel.conversations.content_blocks import TextBlock
from pickel.conversations.conversation_service import ConversationService
from pickel.model_calls.content import (
    RequestContent,
    ResponseContent,
    encode_request_content,
    encode_response_content,
)
from pickel.model_calls.model_call import ModelCall, ModelCallError
from pickel.observe.model_call_content_reader import ModelCallContentReader
from pickel.observe.operation_fact_reader import OperationFactReader
from pickel.observe.operation_projector import OperationObservationProjector
from pickel.operations.agent_run_state import AgentRunState
from pickel.operations.session_operation import SessionOperation
from pickel.persistence.in_memory_runtime_store import InMemoryRuntimeStore
from pickel.shared.execution_identity import ExecutionIdentity
from pickel.workspaces.workspace_binding import WorkspaceBinding


def test_operation_projector_end_to_end_projection(tmp_path: Path) -> None:
    store = InMemoryRuntimeStore()
    content_store = store.model_call_content_store
    now = datetime(2026, 8, 27, 10, 0, 0, tzinfo=timezone.utc)

    # 1. 准备持久化 Request / Response 内容
    req1 = RequestContent(
        model_context=ModelContext(
            system=SystemContent.from_text("system instruction"),
            messages=(UserMessage((TextBlock("hello"),)),),
            tools=(),
        ),
        wire_request={
            "model": "gpt-4o",
            "messages": [{"role": "user", "content": "hello"}],
        },
    )
    req1_ref = content_store.put(encode_request_content(req1))

    resp1 = ResponseContent(
        partial=False,
        provider_response={
            "id": "resp_1",
            "usage": {"prompt_tokens": 100, "completion_tokens": 20},
        },
        assistant_message=AssistantMessage(
            (TextBlock("world"),),
            metadata=ModelResponseMetadata(
                provider="openai",
                model="gpt-4o",
                finish_reason="stop",
                usage=ModelUsage(
                    input_tokens=100,
                    output_tokens=20,
                    cache_read_tokens=25,
                    total_tokens=120,
                ),
            ),
        ),
    )
    resp1_ref = content_store.put(encode_response_content(resp1))

    # 2. 准备第二个失败的 ModelCall (Attempt 1) 和 重试成功的 (Attempt 2)
    call1_fail = ModelCall(
        model_call_id="mc_fail_1",
        identity=ExecutionIdentity(
            session_id="sess_proj",
            operation_id="op_proj",
            step_id="step_1",
            step_sequence=1,
            model_call_id="mc_fail_1",
        ),
        request_attempt=1,
        model_role="primary",
        purpose="agent_step",
        provider="openai",
        api_kind="chat_completions",
        endpoint="https://api.openai.com/v1/chat/completions",
        requested_model="gpt-4o",
        returned_model=None,
        status="failed",
        request_content_ref=req1_ref.to_string(),
        response_content_ref=None,
        context_fingerprint="fp_test",
        provider_request_id=None,
        http_status=503,
        error=ModelCallError(code="503", message="Service Unavailable", retryable=True),
        created_at=now,
        started_at=now,
        first_chunk_at=None,
        finished_at=datetime(2026, 8, 27, 10, 0, 1, tzinfo=timezone.utc),
    )

    call1_retry = ModelCall(
        model_call_id="mc_retry_2",
        identity=ExecutionIdentity(
            session_id="sess_proj",
            operation_id="op_proj",
            step_id="step_1",
            step_sequence=1,
            model_call_id="mc_retry_2",
        ),
        request_attempt=2,
        model_role="primary",
        purpose="agent_step",
        provider="openai",
        api_kind="chat_completions",
        endpoint="https://api.openai.com/v1/chat/completions",
        requested_model="gpt-4o",
        returned_model="gpt-4o",
        status="completed",
        request_content_ref=req1_ref.to_string(),
        response_content_ref=resp1_ref.to_string(),
        context_fingerprint="fp_test",
        provider_request_id="chatcmpl-123",
        http_status=200,
        error=None,
        created_at=now,
        started_at=datetime(2026, 8, 27, 10, 0, 2, tzinfo=timezone.utc),
        first_chunk_at=datetime(2026, 8, 27, 10, 0, 3, tzinfo=timezone.utc),
        finished_at=datetime(2026, 8, 27, 10, 0, 5, tzinfo=timezone.utc),
    )

    # 3. 填充 store
    conv_service = ConversationService(
        store,
        session_id_factory=lambda: "sess_proj",
        node_id_factory=iter(("user_n1", "asst_n1")).__next__,
        now=lambda: now,
    )
    conv_service.create_conversation_session(agent_id="Pickle", cwd=str(tmp_path))
    conv_service.append_user_message(
        session_id="sess_proj",
        message=UserMessage((TextBlock("hello"),)),
    )

    op = SessionOperation(
        operation_id="op_proj",
        session_id="sess_proj",
        agent_package_version_id="pkg_v1",
        workspace_binding=WorkspaceBinding(
            workspace_id="ws_01",
            working_directory=tmp_path,
            allowed_root=None,
        ),
        input_node_id="user_n1",
        accepted_at=now,
    )
    store._operations[op.operation_id] = op
    store._model_call_rows()[call1_fail.model_call_id] = call1_fail
    store._model_call_rows()[call1_retry.model_call_id] = call1_retry

    run_state = AgentRunState(
        operation_id="op_proj",
        revision=2,
        status="succeeded",
        waiting_reason=None,
        completed_step_count=1,
        current_step=None,
        final_assistant_node_id="asst_n1",
        error=None,
        cancellation=None,
    )
    store._run_states[op.operation_id] = run_state

    # 4. 执行投影
    fact_reader = OperationFactReader(store)
    content_reader = ModelCallContentReader(content_store)
    projector = OperationObservationProjector(fact_reader, content_reader)

    doc = projector.project_operation("op_proj")

    # 验证关键结构与数据
    assert doc.operation["operation_id"] == "op_proj"
    assert doc.summary["status"] == "succeeded"
    assert doc.summary["model_calls_count"] == 2
    assert doc.summary["model_retries_count"] == 1

    # 验证 ModelCalls
    calls = doc.model_calls
    assert len(calls) == 2
    assert calls[0].attempt == 1
    assert calls[0].status == "failed"
    assert calls[0].http_status == 503

    assert calls[1].attempt == 2
    assert calls[1].status == "completed"
    assert calls[1].usage.input_tokens == 100
    assert calls[1].usage.cache_read_tokens == 25
    assert calls[1].usage.cache_hit_rate == 25.0  # 25 / 100 * 100%
    assert calls[1].usage.cache_hit_rate_formula == "cache_read_tokens / input_tokens"
    assert calls[1].usage.cache_hit_rate_denominator == 100
    assert calls[1].usage.cache_hit_rate_source == "assistant_message.metadata.usage"

    # 验证执行树节点
    node_keys = [n.key for n in doc.execution_nodes]
    assert "operation" in node_keys
    assert "step_1" in node_keys
    assert calls[0].key in node_keys
    assert calls[1].key in node_keys

    # 验证图表数据
    assert len(doc.charts["latency"]) == 2
    assert len(doc.charts["cache"]) == 2
    assert len(doc.charts["tokens"]) == 2

    # 验证不可变证据
    evidence = doc.document_evidence[calls[1].key]
    assert evidence["model_call_id"] == "mc_retry_2"
    assert len(evidence["context"]["sections"]) >= 2
    assert len(evidence["wire"]["sections"]) >= 1
    assert len(evidence["provider"]["sections"]) >= 1
    assert len(evidence["assistant"]["sections"]) >= 1
    provider_body = next(
        section
        for section in evidence["provider"]["sections"]
        if section["id"] == "provider-body"
    )
    assert isinstance(provider_body["value"], dict)
    assert provider_body["value"]["id"] == "resp_1"

    # 验证序列化
    json_doc = doc.to_json()
    assert "op_proj" in json_doc
    assert "mc_retry_2" in json_doc
