from datetime import datetime, timezone
from pathlib import Path
from inspect import signature

import pytest

from pickel.artifacts.artifact import Artifact, ArtifactReference
from pickel.conversations.agent_message import (
    AssistantMessage,
    ToolResultMessage,
    UserMessage,
)
from pickel.conversations.content_blocks import TextBlock, ToolCallBlock
from pickel.context.model_context import (
    ModelContext,
    SystemContent,
    ToolDefinition,
)
from pickel.conversations.conversation_node import (
    ConversationNode,
    HistoryCompaction,
)
from pickel.conversations.conversation_session import ConversationSession
from pickel.inbox.message import InboxMessage, UserMessageSource
from pickel.inbox.store import InboxStore
from pickel.operations.agent_run_state import (
    AgentRunState,
    DelegateAgentIntent,
    ModelRequestIntent,
    ModelStepState,
    ToolCallState,
    ToolApproval,
    ToolApprovalDecision,
)
from pickel.operations.session_operation import SessionOperation
from pickel.shared.frozen_json import freeze_json
from pickel.workspaces.workspace import Workspace
from pickel.workspaces.workspace_binding import WorkspaceBinding
from pickel.workspaces.store import WorkspaceStore
from pickel.conversations.conversation_store import ConversationStore

NOW = datetime(2026, 8, 25, tzinfo=timezone.utc)
ARTIFACT_ID = "artifact_" + "0" * 64


def test_session_has_only_v10_fields(tmp_path: Path) -> None:
    session = ConversationSession(
        "session_1",
        "agent_1",
        "workspace_1",
        tmp_path,
        None,
        None,
        None,
        None,
        NOW,
        NOW,
        None,
    )
    assert not hasattr(session, "status")
    assert not hasattr(session, "current_commit_sequence")
    assert session.cwd == tmp_path.resolve()


def test_conversation_node_strict_content_codec() -> None:
    node = ConversationNode(
        "node_1",
        "session_1",
        None,
        "history_compaction",
        HistoryCompaction("summary", ()),
        NOW,
    )
    restored = ConversationNode.from_content_json(
        node_id=node.node_id,
        session_id=node.session_id,
        parent_node_id=node.parent_node_id,
        content_type=node.content_type,
        content_json=node.content_json(),
        created_at=node.created_at,
    )
    assert restored == node
    with pytest.raises(ValueError, match="字段不匹配"):
        ConversationNode.from_content_json(
            node_id="node_1",
            session_id="session_1",
            parent_node_id=None,
            content_type="history_compaction",
            content_json='{"summary":"x","retained_messages":[],"extra":1}',
            created_at=NOW,
        )


def test_conversation_node_ignores_historical_structured_tool_content() -> None:
    node = ConversationNode.from_content_json(
        node_id="node_1",
        session_id="session_1",
        parent_node_id=None,
        content_type="agent_message",
        content_json=(
            '{"payload_version":3,"role":"tool","tool_call_id":"c1",'
            '"tool_name":"echo","content":[{"type":"text","text":"ok"}],'
            '"is_error":false,"structured_content":{"duplicate":true}}'
        ),
        created_at=NOW,
    )

    assert node.content_dict() == {
        "payload_version": 3,
        "role": "tool",
        "tool_call_id": "c1",
        "tool_name": "echo",
        "content": [{"type": "text", "text": "ok"}],
        "is_error": False,
    }


def test_inbox_message_roundtrip_and_state_invariants() -> None:
    message = InboxMessage(
        "message_1",
        "session_1",
        1,
        "followup",
        UserMessage(),
        UserMessageSource(),
        NOW,
    )
    assert InboxMessage.from_json(message.to_json()) == message
    payload = message.message_payload_dict()
    assert set(payload) == {"message", "source"}
    assert "message_id" not in payload
    assert "status" not in payload
    with pytest.raises(ValueError, match="不能有处理结果"):
        InboxMessage(
            "message_1",
            "session_1",
            1,
            "followup",
            UserMessage(),
            UserMessageSource(),
            NOW,
            claimed_operation_id="operation_1",
        )


def test_run_state_uses_revision_and_exact_phases() -> None:
    context = ModelRequestIntent(
        ModelContext(
            SystemContent.from_text("system"),
            [UserMessage()],
            [ToolDefinition("echo", "Echo", {"type": "object"}, {"type": "string"})],
        ),
        "fp",
    )
    step = ModelStepState("step_1", 1, "request_ready", 0, context, None, ())
    state = AgentRunState("operation_1", 1, "running", None, 0, step, None, None, None)
    assert AgentRunState.from_json(state.to_json()) == state
    with pytest.raises(ValueError, match="必须有 request_intent"):
        ModelStepState("step_1", 1, "request_ready", 0, None, None, ())

    restored_context = ModelContext.from_json(context.model_context.to_json())
    assert restored_context == context.model_context
    with pytest.raises(ValueError, match="字段不匹配"):
        ModelContext.from_json('{"system":{},"messages":[],"tools":[],"extra":1}')


def test_model_context_deep_freezes_messages_and_tool_schema() -> None:
    blocks = [ToolCallBlock("call", "echo", {"nested": {"value": 1}})]
    rendered = TextBlock("rendered")
    message = AssistantMessage(content=blocks)
    tool_message = ToolResultMessage("call", "echo", content=[rendered])
    context = ModelContext(
        SystemContent(),
        [message, tool_message],
        [
            ToolDefinition(
                "echo",
                "Echo",
                {"properties": {"x": {"type": "string"}}},
                {"type": "string"},
            )
        ],
    )
    blocks.append(ToolCallBlock("other", "other", {}))
    assert len(context.messages[0].content) == 1
    assert context.messages[1].content[0].text == "rendered"
    with pytest.raises(AttributeError):
        context.messages[0].content.append(TextBlock("nope"))
    with pytest.raises(TypeError):
        context.messages[0].content[0].arguments["x"] = {}


def test_delegate_intent_codec_and_approval_combinations() -> None:
    intent = DelegateAgentIntent("package_child")
    call = ToolCallState(
        "tool_1",
        "delegate_agent",
        {},
        "intent_recorded",
        None,
        "never",
        intent,
        None,
        None,
        None,
    )
    state = ModelStepState("step_1", 1, "awaiting_tools", 0, None, "node_1", (call,))
    assert AgentRunState.from_json(
        AgentRunState(
            "operation_1", 1, "running", None, 0, state, None, None, None
        ).to_json()
    )
    with pytest.raises(ValueError, match="execution_intent"):
        AgentRunState.from_json(
            AgentRunState("operation_1", 1, "running", None, 0, state, None, None, None)
            .to_json()
            .replace('"kind":"delegate_agent"', '"kind":"generic"')
        )
    denied = ToolApproval(
        NOW,
        "tool_policy",
        "no",
        ToolApprovalDecision("denied", NOW, "actor", "no"),
    )
    with pytest.raises(ValueError, match="denied approval"):
        ToolCallState(
            "tool_2", "echo", {}, "ready", denied, "safe", None, None, None, None
        )


def test_rejected_tool_call_requires_reason_and_has_no_intent_or_result() -> None:
    rejected = ToolCallState(
        "tool_3", "echo", {}, "rejected", None, "safe", None, "hook denied", None, None
    )
    assert rejected.status == "rejected"

    with pytest.raises(ValueError, match="rejected 必须有 rejected 原因"):
        ToolCallState(
            "tool_4", "echo", {}, "rejected", None, "safe", None, None, None, None
        )
    with pytest.raises(ValueError, match="rejected 不能有 execution_intent"):
        ToolCallState(
            "tool_5",
            "echo",
            {},
            "rejected",
            None,
            "safe",
            DelegateAgentIntent("package-child"),
            "hook denied",
            None,
            None,
        )


def test_operation_and_workspace_binding_are_frozen(tmp_path: Path) -> None:
    workspace = Workspace("workspace_1", tmp_path, NOW)
    binding = WorkspaceBinding(workspace.workspace_id, workspace.root_path, None)
    operation = SessionOperation(
        "operation_1", "session_1", "package_1", binding, "node_1", NOW
    )
    assert SessionOperation.from_json(operation.to_json()) == operation
    assert not hasattr(operation, "operation_type")
    assert not hasattr(operation, "accepted_commit_sequence")


def test_workspace_can_load_after_directory_disappears(tmp_path: Path) -> None:
    workspace = Workspace("workspace_1", tmp_path / "removed", NOW)
    assert workspace.root_path == (tmp_path / "removed").resolve()


def test_artifact_metadata_does_not_duplicate_reference_data() -> None:
    artifact = Artifact(ARTIFACT_ID, 4, NOW)
    reference = ArtifactReference(ARTIFACT_ID, "text/plain", "note.txt")
    assert set(artifact.to_dict()) == {"artifact_id", "size_bytes", "created_at"}
    assert set(reference.to_dict()) == {"artifact_id", "media_type", "display_name"}
    assert not hasattr(artifact, "digest")
    assert not hasattr(reference, "size_bytes")


def test_store_ports_keep_atomic_boundaries_narrow() -> None:
    assert "insert_message" not in InboxStore.__dict__
    assert "send_message" in InboxStore.__dict__
    assert "insert_node" not in ConversationStore.__dict__
    assert "append_node" in ConversationStore.__dict__
    assert "create_workspace" not in WorkspaceStore.__dict__
    assert "workspace" in signature(ConversationStore.create_session).parameters


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_frozen_json_rejects_non_json_numbers(value: float) -> None:
    with pytest.raises(ValueError, match="NaN 或 Infinity"):
        freeze_json(value)
