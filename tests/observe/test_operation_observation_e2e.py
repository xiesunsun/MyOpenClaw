from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from pickel.agents.agent_package import (
    AgentRuntimePolicy,
    ImplementationRef,
    ModelPolicy,
    ModelVersion,
    WorkspacePolicy,
    build_agent_package_version,
)
from pickel.context.model_context import ModelContext, SystemContent
from pickel.conversations.agent_message import (
    AssistantMessage,
    ModelResponseMetadata,
    ModelUsage,
    UserMessage,
)
from pickel.conversations.content_blocks import TextBlock
from pickel.conversations.conversation_service import ConversationService
from pickel.inbox.message import UserMessageSource
from pickel.model_calls.content import (
    RequestContent,
    ResponseContent,
    encode_request_content,
    encode_response_content,
)
from pickel.model_calls.model_call import ModelCall
from pickel.observe.operation_report import export_operation_observation
from pickel.operations.agent_run_state import AgentRunState
from pickel.operations.session_operation import SessionOperation
from pickel.persistence.sqlite_runtime_store import SQLiteRuntimeStore
from pickel.shared.execution_identity import ExecutionIdentity
from pickel.workspaces.workspace_binding import WorkspaceBinding


def test_operation_observation_e2e_sqlite(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime.sqlite3"
    store = SQLiteRuntimeStore(db_path)
    content_store = store.model_call_content_store

    now = datetime.now(timezone.utc)

    # 1. 创建 Session
    conv_service = ConversationService(
        store,
        session_id_factory=lambda: "sess_e2e_01",
        node_id_factory=iter(("user_node_1", "asst_node_1")).__next__,
        now=lambda: now,
    )
    session = conv_service.create_conversation_session(
        agent_id="Pickle", cwd=str(tmp_path)
    )

    # 2. 发送并接受 InboxMessage
    inbox_msg = store.send_message(
        message_id="msg_e2e_01",
        session_id=session.session_id,
        delivery="followup",
        message=UserMessage((TextBlock("运行诊断任务"),)),
        source=UserMessageSource(),
        created_at=now,
    )

    pkg = build_agent_package_version(
        agent_id="Pickle",
        format_version=1,
        behavior_instruction="Test agent behavior",
        model_policy=ModelPolicy(
            primary=ModelVersion(
                provider="anthropic",
                model="claude-3-7-sonnet",
                wire_protocol="anthropic_messages",
                api_base=None,
                temperature=None,
                max_input_tokens=None,
                max_output_tokens=1000,
                provider_options={},
                provider_implementation=ImplementationRef("provider", "anthropic"),
                required_secret_refs=(),
            )
        ),
        runtime_policy=AgentRuntimePolicy(max_model_steps=5, context_turn_window=10),
        workspace_policy=WorkspacePolicy("workspace"),
        skills=(),
        tools=(),
        extensions=(),
        created_at=now,
    )
    store.insert_agent_package_version(pkg)

    op = SessionOperation(
        operation_id="op_e2e_01",
        session_id=session.session_id,
        agent_package_version_id=pkg.package_version_id,
        workspace_binding=WorkspaceBinding(
            workspace_id=session.workspace_id,
            working_directory=tmp_path,
            allowed_root=None,
        ),
        input_node_id=inbox_msg.message_id,
        accepted_at=now,
    )
    initial_state = AgentRunState(
        operation_id=op.operation_id,
        revision=1,
        status="queued",
        waiting_reason=None,
        completed_step_count=0,
        current_step=None,
        final_assistant_node_id=None,
        error=None,
        cancellation=None,
    )
    accepted = store.accept_operation(
        operation=op,
        state=initial_state,
        expected_node_id=None,
    )
    assert accepted is True

    # 3. 准备 Request/Response 内容
    req1 = RequestContent(
        model_context=ModelContext(
            system=SystemContent.from_text("system instruction"),
            messages=(UserMessage((TextBlock("运行诊断任务"),)),),
            tools=(),
        ),
        wire_request={"model": "claude-3-7-sonnet", "stream": True, "messages": []},
    )
    req1_ref = content_store.put(encode_request_content(req1))

    resp1 = ResponseContent(
        partial=False,
        provider_response={
            "id": "msg_01",
            "usage": {"input_tokens": 800, "output_tokens": 150},
        },
        assistant_message=AssistantMessage(
            (TextBlock("诊断已完成"),),
            metadata=ModelResponseMetadata(
                provider="anthropic",
                model="claude-3-7-sonnet",
                finish_reason="stop",
                usage=ModelUsage(
                    input_tokens=800,
                    output_tokens=150,
                    cache_read_tokens=320,
                    total_tokens=950,
                ),
            ),
        ),
    )
    resp1_ref = content_store.put(encode_response_content(resp1))

    # 4. 插入 ModelCall 到 SQLite
    call1 = ModelCall(
        model_call_id="mc_e2e_01",
        identity=ExecutionIdentity(
            session_id=session.session_id,
            operation_id=op.operation_id,
            step_id="step_1",
            step_sequence=1,
            model_call_id="mc_e2e_01",
        ),
        request_attempt=1,
        model_role="primary",
        purpose="agent_step",
        provider="anthropic",
        api_kind="messages",
        endpoint="https://api.anthropic.com/v1/messages",
        requested_model="claude-3-7-sonnet",
        returned_model="claude-3-7-sonnet",
        status="completed",
        request_content_ref=req1_ref.to_string(),
        response_content_ref=resp1_ref.to_string(),
        context_fingerprint="fp_e2e",
        provider_request_id="msg_01",
        http_status=200,
        error=None,
        created_at=now,
        started_at=now,
        first_chunk_at=now,
        finished_at=now,
    )
    with store._connect() as conn:
        conn.execute(
            """
            INSERT INTO model_calls VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                call1.model_call_id,
                call1.session_id,
                call1.operation_id,
                call1.step_id,
                call1.step_sequence,
                call1.request_attempt,
                call1.model_role,
                call1.purpose,
                call1.provider,
                call1.api_kind,
                call1.endpoint,
                call1.requested_model,
                call1.returned_model,
                call1.status,
                call1.request_content_ref,
                call1.response_content_ref,
                call1.context_fingerprint,
                call1.provider_request_id,
                call1.http_status,
                None,
                call1.created_at.isoformat(),
                call1.started_at.isoformat() if call1.started_at else None,
                call1.first_chunk_at.isoformat() if call1.first_chunk_at else None,
                call1.finished_at.isoformat() if call1.finished_at else None,
            ),
        )

    # 5. 导出 HTML 报告
    html_target = tmp_path / "e2e_report.html"
    exported_html = export_operation_observation(
        operation_id=op.operation_id,
        store=store,
        content_store=content_store,
        out=html_target,
        format="html",
    )
    assert exported_html == html_target
    html_content = html_target.read_text(encoding="utf-8")
    assert "Pickel Diagnostics" in html_content
    assert "mc_e2e_01" in html_content
    assert "claude-3-7-sonnet" in html_content

    # 6. 导出 JSON 报告
    json_target = tmp_path / "e2e_report.json"
    exported_json = export_operation_observation(
        operation_id=op.operation_id,
        store=store,
        content_store=content_store,
        out=json_target,
        format="json",
    )
    assert exported_json == json_target
    json_content = json.loads(json_target.read_text(encoding="utf-8"))
    assert json_content["operation"]["operation_id"] == op.operation_id
    assert len(json_content["model_calls"]) == 1
    # Anthropic input_tokens 不包含 cache read，实际输入分母为 800 + 320。
    assert json_content["model_calls"][0]["usage"]["cache_hit_rate"] == 28.57
    assert json_content["model_calls"][0]["usage"]["cache_hit_rate_denominator"] == 1120
    evidence = json_content["document_evidence"]["call1"]
    assert evidence["context"]["schema_version"] == 1
    assert evidence["context"]["canonical_bytes_verified"] is True
    assert evidence["provider"]["schema_version"] == 1
    assert evidence["provider"]["canonical_bytes_verified"] is True
