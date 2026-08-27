from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from pickel.app.runtime_host import RuntimeHost
from pickel.app.runtime_models import ConversationRequest
from pickel.context.model_context import ModelContext, SystemContent
from pickel.conversations.agent_message import UserMessage
from pickel.conversations.content_blocks import TextBlock
from pickel.inbox.message import UserMessageSource
from pickel.operations.agent_run_state import ModelRequestIntent, ModelStepState
from pickel.operations.operation_service import OperationService
from pickel.workspaces.workspace_binding import WorkspaceBinding
from tests.app.test_runtime_host import _boot


def test_context_inspection_reads_committed_model_request_intent(tmp_path, monkeypatch):
    monkeypatch.setenv("PICKEL_HOME", str(tmp_path / "home"))
    # 复用 RuntimeHost 测试的最小 Package 配置。
    host = RuntimeHost(_boot(tmp_path))
    conversation = host.open_conversation(
        ConversationRequest(agent_id="Pickle", cwd=tmp_path)
    )
    store = conversation.persistence_store
    now = datetime(2026, 8, 25, tzinfo=timezone.utc)
    message = store.send_message(
        message_id="message-1",
        session_id=conversation.session.session_id,
        delivery="followup",
        message=UserMessage((TextBlock("input"),)),
        source=UserMessageSource(),
        created_at=now,
    )
    operation_service = OperationService(store)
    accepted = operation_service.accept_pending_message(
        message=message,
        agent_package_version_id=conversation.agent_definition.package_version_id,
        workspace_binding=WorkspaceBinding(
            workspace_id=conversation.session.workspace_id,
            working_directory=conversation.session.cwd,
            allowed_root=conversation.session.cwd,
        ),
        expected_node_id=conversation.session.active_node_id,
        accepted_at=now,
    )
    assert accepted is not None

    step = ModelStepState(
        step_id="step-1",
        step_sequence=1,
        phase="preparing_request",
        request_attempt=0,
        request_intent=None,
        assistant_message_node_id=None,
        tool_calls=(),
    )
    running = replace(
        accepted.state,
        revision=accepted.state.revision + 1,
        status="running",
        current_step=step,
    )
    assert operation_service.commit_state(
        state=running, expected_revision=accepted.state.revision
    )
    context = ModelContext(
        system=SystemContent.from_text("committed system"),
        messages=(UserMessage((TextBlock("committed intent"),)),),
    )
    ready_step = replace(
        step,
        phase="request_ready",
        request_intent=ModelRequestIntent(context, "fingerprint"),
    )
    ready = replace(running, revision=running.revision + 1, current_step=ready_step)
    assert operation_service.commit_state(
        state=ready, expected_revision=running.revision
    )

    with (
        patch(
            "pickel.app.conversation_runtime.ConversationProjector.project_conversation_messages",
            side_effect=AssertionError("不应重建 Intent Context"),
        ),
        patch(
            "pickel.app.conversation_runtime.ModelContextBuilder.build_model_context",
            side_effect=AssertionError("不应重建 Intent Context"),
        ),
    ):
        inspection = asyncio.run(conversation.inspect_context())

    assert inspection.source == "model_request_intent"
    assert inspection.usage.total_tokens > 0
    asyncio.run(host.shutdown())


def test_context_inspection_marks_uncommitted_context_as_preview(tmp_path, monkeypatch):
    monkeypatch.setenv("PICKEL_HOME", str(tmp_path / "home"))
    host = RuntimeHost(_boot(tmp_path))
    conversation = host.open_conversation(
        ConversationRequest(agent_id="Pickle", cwd=tmp_path)
    )

    inspection = asyncio.run(conversation.inspect_context())

    assert inspection.source == "preview"
    asyncio.run(host.shutdown())


def test_context_inspection_never_uses_utility_model_call_as_context_source(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("PICKEL_HOME", str(tmp_path / "home"))
    host = RuntimeHost(_boot(tmp_path))
    conversation = host.open_conversation(
        ConversationRequest(agent_id="Pickle", cwd=tmp_path)
    )
    with patch.object(
        conversation.persistence_store,
        "list_model_calls",
        return_value=(SimpleNamespace(purpose="title", operation_id=None),),
    ) as list_calls:
        inspection = asyncio.run(conversation.inspect_context())

    assert inspection.source == "preview"
    list_calls.assert_called_once_with(session_id=conversation.session.session_id)
    asyncio.run(host.shutdown())
