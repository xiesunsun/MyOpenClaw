from __future__ import annotations

from pathlib import Path
import asyncio
from types import SimpleNamespace

from pickel.context.history_compaction import plan_history_compaction_for_budget
from pickel.runtime.history_compaction import HistoryCompactionService
from pickel.context.projection import ConversationProjector
from pickel.conversations.agent_message import (
    AssistantMessage,
    ToolResultMessage,
    UserMessage,
)
from pickel.conversations.content_blocks import TextBlock, ToolCallBlock
from pickel.conversations.conversation_node import HistoryCompaction
from pickel.conversations.conversation_service import ConversationService
from pickel.providers.prepared import PreparedModelCall
from pickel.model_calls.service import ModelCallService
from pickel.persistence.in_memory_runtime_store import InMemoryRuntimeStore
from pickel.providers.stream import StreamCompleted
from pickel.runtime.runtime_effects import RuntimeEffects
from pickel.runtime.model_call_send_gate import ModelCallSendGate


def _conversation(tmp_path: Path) -> ConversationService:
    values = iter(f"node-{index}" for index in range(1, 30))
    return ConversationService(
        InMemoryRuntimeStore(),
        session_id_factory=lambda: "session-1",
        node_id_factory=values.__next__,
    )


def test_budget_selector_preserves_complete_message_units(tmp_path: Path):
    service = _conversation(tmp_path)
    session = service.create_conversation_session(agent_id="Pickle", cwd=str(tmp_path))
    for index in range(3):
        service.append_user_message(
            session_id=session.session_id,
            message=UserMessage(content=[TextBlock(text=f"u{index}")]),
        )
        service.append_assistant_message(
            session_id=session.session_id,
            message=AssistantMessage(content=[TextBlock(text=f"a{index}")]),
        )

    nodes = service.list_active_branch_nodes(session_id=session.session_id)
    plan = plan_history_compaction_for_budget(nodes, target_token_budget=180)

    assert plan is not None
    assert plan.first_kept_node_id == "node-5"
    assert [message.content[0].text for message in plan.messages] == [
        "u0",
        "a0",
        "u1",
        "a1",
    ]


def test_multiple_compaction_epochs_project_latest_summary_and_tail(tmp_path: Path):
    service = _conversation(tmp_path)
    session = service.create_conversation_session(agent_id="Pickle", cwd=str(tmp_path))
    for index in range(4):
        service.append_user_message(
            session_id=session.session_id,
            message=UserMessage(content=[TextBlock(text=f"u{index}")]),
        )
        service.append_assistant_message(
            session_id=session.session_id,
            message=AssistantMessage(content=[TextBlock(text=f"a{index}")]),
        )
    first = plan_history_compaction_for_budget(
        service.list_active_branch_nodes(session_id=session.session_id),
        target_token_budget=180,
    )
    assert first is not None
    service.append_history_compaction(
        session_id=session.session_id,
        content=HistoryCompaction("epoch one", first.first_kept_node_id),
    )
    for index in range(4, 6):
        service.append_user_message(
            session_id=session.session_id,
            message=UserMessage(content=[TextBlock(text=f"u{index}")]),
        )
        service.append_assistant_message(
            session_id=session.session_id,
            message=AssistantMessage(content=[TextBlock(text=f"a{index}")]),
        )

    second = plan_history_compaction_for_budget(
        service.list_active_branch_nodes(session_id=session.session_id),
        target_token_budget=180,
    )
    assert second is not None
    assert any(
        getattr(block, "text", "") == "[compaction]\nepoch one"
        for message in second.messages
        for block in message.content
    )
    committed = service.append_history_compaction(
        session_id=session.session_id,
        content=HistoryCompaction("epoch two", second.first_kept_node_id),
    )
    messages = ConversationProjector().project_conversation_messages(
        service.list_active_branch_nodes(
            session_id=session.session_id,
        )
    )
    assert [message.content[0].text for message in messages] == [
        "[compaction]\nepoch two",
        "u5",
        "a5",
    ]
    assert committed.content.first_kept_node_id == second.first_kept_node_id


class _Worker:
    def __init__(self):
        self.bodies = []

    def prepare(self, context):
        self.bodies.append(context.to_dict())
        return PreparedModelCall(
            provider="worker",
            api_kind="test",
            endpoint="generate",
            requested_model="worker-model",
            body={"messages": context.to_dict()["messages"]},
        )

    async def stream_prepared(self, prepared):
        yield StreamCompleted(
            AssistantMessage(content=[TextBlock(text="stable summary")]),
            provider_response={"id": "compaction-1"},
            http_status=200,
        )


def test_compaction_service_uses_worker_model_call_and_keeps_tree(tmp_path: Path):
    assert not hasattr(ModelCallService, "store")

    seen_effects = []

    class _RecordingEffects(RuntimeEffects):
        async def execute_prepared_model_call(self, **kwargs):
            seen_effects.append(self)
            return await super().execute_prepared_model_call(**kwargs)

    store = InMemoryRuntimeStore()
    limiter = asyncio.Semaphore(1)
    service = ConversationService(
        store,
        session_id_factory=lambda: "session-1",
        node_id_factory=iter(f"node-{index}" for index in range(1, 30)).__next__,
    )
    session = service.create_conversation_session(agent_id="Pickle", cwd=str(tmp_path))
    for index in range(3):
        service.append_user_message(
            session_id=session.session_id,
            message=UserMessage(content=[TextBlock(text=f"u{index}")]),
        )
        if index == 0:
            service.append_assistant_message(
                session_id=session.session_id,
                message=AssistantMessage(
                    content=[
                        TextBlock(text="a0"),
                        ToolCallBlock(id="call-0", name="echo", arguments={"x": 1}),
                    ]
                ),
            )
            service.append_tool_result_message(
                session_id=session.session_id,
                message=ToolResultMessage(
                    tool_call_id="call-0",
                    tool_name="echo",
                    content=[TextBlock(text="result")],
                ),
            )
        else:
            service.append_assistant_message(
                session_id=session.session_id,
                message=AssistantMessage(content=[TextBlock(text=f"a{index}")]),
            )
    nodes = service.list_active_branch_nodes(session_id=session.session_id)
    worker = _Worker()
    model_calls = ModelCallService(store)
    package = SimpleNamespace(
        model_policy=SimpleNamespace(
            worker=SimpleNamespace(provider="worker", model="worker-model")
        )
    )

    committed = asyncio.run(
        HistoryCompactionService(
            conversation_service=service,
            model_calls=model_calls,
            send_gate=ModelCallSendGate(store),
        ).compact(
            session_id=session.session_id,
            nodes=nodes,
            package=package,
            effects=_RecordingEffects(
                provider=worker,
                worker_provider=worker,
                provider_timeout_seconds=7,
                model_request_limiter=limiter,
            ),
            target_token_budget=180,
        )
    )

    assert committed.content.summary == "stable summary"
    assert (
        len(
            store.list_branch_nodes(
                session_id=session.session_id, leaf_node_id=committed.node_id
            )
        )
        == 8
    )
    calls = store.list_model_calls(session_id=session.session_id)
    assert len(calls) == 1
    assert calls[0].model_role == "worker"
    assert calls[0].purpose == "history_compaction"
    assert '"tool_call_id"' in worker.bodies[0]["messages"][0]["content"][0]["text"]
    assert seen_effects[0].provider is worker
    assert seen_effects[0].provider_timeout_seconds == 7
    assert seen_effects[0].model_request_limiter is limiter
