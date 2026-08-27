from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from pickel.context.model_context import ModelContext, SystemContent
from pickel.conversations.agent_message import (
    AssistantMessage,
    ModelResponseMetadata,
    ModelUsage,
    ToolResultMessage,
    UserMessage,
)
from pickel.conversations.content_blocks import TextBlock, ToolCallBlock
from pickel.conversations.conversation_node import ConversationNode
from pickel.conversations.conversation_service import ConversationService
from pickel.model_calls.content import (
    RequestContent,
    ResponseContent,
    encode_request_content,
    encode_response_content,
)
from pickel.model_calls.model_call import ModelCall
from pickel.observe.operation_fact_reader import OperationFactReader
from pickel.operations.session_operation import SessionOperation
from pickel.persistence.in_memory_runtime_store import InMemoryRuntimeStore
from pickel.shared.execution_identity import ExecutionIdentity
from pickel.workspaces.workspace_binding import WorkspaceBinding


def test_operation_fact_reader_reads_all_facts(tmp_path: Path) -> None:
    store = InMemoryRuntimeStore()
    now = datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)

    # 1. 创建 Session
    conv_service = ConversationService(
        store,
        session_id_factory=lambda: "sess_01",
        node_id_factory=iter(("node_user_1", "node_asst_1")).__next__,
        now=lambda: now,
    )
    conv_service.create_conversation_session(agent_id="Pickle", cwd=str(tmp_path))
    conv_service.append_user_message(
        session_id="sess_01",
        message=UserMessage((TextBlock("test message"),)),
    )

    # 2. 插入 Operation
    op = SessionOperation(
        operation_id="op_01",
        session_id="sess_01",
        agent_package_version_id="pkg_v1",
        workspace_binding=WorkspaceBinding(
            workspace_id="ws_01",
            working_directory=tmp_path,
            allowed_root=None,
        ),
        input_node_id="node_user_1",
        accepted_at=now,
    )
    store._operations[op.operation_id] = op

    # 3. 准备 Request / Response 内容
    req = RequestContent(
        model_context=ModelContext(
            system=SystemContent.from_text("sys"),
            messages=(),
            tools=(),
        ),
        wire_request={"model": "test-model", "messages": []},
    )
    req_ref = store.model_call_content_store.put(encode_request_content(req))

    resp = ResponseContent(
        partial=False,
        provider_response={"choices": [{"message": {"content": "ok"}}]},
        assistant_message=AssistantMessage(
            (TextBlock("ok"),),
            metadata=ModelResponseMetadata(
                provider="anthropic",
                model="claude-3-7-sonnet",
                finish_reason="stop",
                usage=ModelUsage(
                    input_tokens=100,
                    output_tokens=20,
                ),
            ),
        ),
    )
    resp_ref = store.model_call_content_store.put(encode_response_content(resp))

    # 4. 插入 ModelCall
    call1 = ModelCall(
        model_call_id="mc_01",
        identity=ExecutionIdentity(
            session_id="sess_01",
            operation_id="op_01",
            step_id="step_1",
            step_sequence=1,
            model_call_id="mc_01",
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
        request_content_ref=req_ref.to_string(),
        response_content_ref=resp_ref.to_string(),
        context_fingerprint="fp_123",
        provider_request_id="req_01",
        http_status=200,
        error=None,
        created_at=now,
        started_at=now,
        first_chunk_at=now,
        finished_at=now,
    )
    store._model_call_rows()[call1.model_call_id] = call1

    reader = OperationFactReader(store)

    session = reader.read_session("sess_01")
    assert session is not None
    assert session.session_id == "sess_01"

    facts = reader.read_operation_facts("op_01")
    assert facts is not None
    assert facts.operation.operation_id == "op_01"
    assert len(facts.model_calls) == 1
    assert facts.model_calls[0].model_call_id == "mc_01"
    assert facts.input_node is not None
    assert facts.input_node.node_id == "node_user_1"

    # 读取不存在的 operation 返回 None
    assert reader.read_operation_facts("op_not_exist") is None


def test_operation_fact_reader_cuts_operation_branch_and_pairs_tool_result(
    tmp_path: Path,
) -> None:
    store = InMemoryRuntimeStore()
    now = datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)
    conv_service = ConversationService(
        store,
        session_id_factory=lambda: "sess_tools",
        node_id_factory=iter(("input",)).__next__,
        now=lambda: now,
    )
    conv_service.create_conversation_session(agent_id="Pickle", cwd=str(tmp_path))
    conv_service.append_user_message(
        session_id="sess_tools", message=UserMessage((TextBlock("run"),))
    )
    assistant = ConversationNode(
        node_id="assistant_tool",
        session_id="sess_tools",
        parent_node_id="input",
        content_type="agent_message",
        content=AssistantMessage(
            (ToolCallBlock(id="tc_1", name="echo", arguments={"x": 1}),)
        ),
        created_at=now,
    )
    result = ConversationNode(
        node_id="result_tool",
        session_id="sess_tools",
        parent_node_id="assistant_tool",
        content_type="agent_message",
        content=ToolResultMessage(
            tool_call_id="tc_1", tool_name="echo", content=(TextBlock("ok"),)
        ),
        created_at=now,
    )
    store._nodes[assistant.node_id] = assistant
    store._nodes[result.node_id] = result
    store._sessions["sess_tools"] = replace(
        store._sessions["sess_tools"], active_node_id=result.node_id
    )
    op = SessionOperation(
        operation_id="op_tools",
        session_id="sess_tools",
        agent_package_version_id="pkg_v1",
        workspace_binding=WorkspaceBinding(
            workspace_id="ws_01", working_directory=tmp_path, allowed_root=None
        ),
        input_node_id="input",
        accepted_at=now,
    )
    store._operations[op.operation_id] = op

    facts = OperationFactReader(store).read_operation_facts(op.operation_id)
    assert facts is not None
    assert [node.node_id for node in facts.branch_nodes] == [
        "input",
        "assistant_tool",
        "result_tool",
    ]
    assert len(facts.tool_calls) == 1
    tool = facts.tool_calls[0]
    assert tool.tool_call_id == "tc_1"
    assert tool.arguments == {"x": 1}
    assert tool.result is not None and tool.result["role"] == "tool"
    assert tool.is_error is False
    assert tool.source == "conversation_node"
    assert tool.reliability == "fact"
