import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

from pickel.context.history_compaction import ModelBackedHistoryCompactionGenerator
from pickel.context.model_context import ModelContext, SystemContent
from pickel.context.token_preflight import TokenPreflightResult
from pickel.conversations.agent_message import AssistantMessage, UserMessage
from pickel.conversations.content_blocks import TextBlock
from pickel.conversations.conversation_node import ConversationNode
from pickel.model_calls.service import ModelCallResponse
from pickel.providers.prepared import PreparedModelCall
from pickel.runtime.runtime_effects import RuntimeEffects


def _node(node_id: str, message) -> ConversationNode:
    return ConversationNode(
        node_id=node_id,
        session_id="session-1",
        parent_node_id=None,
        content_type="agent_message",
        content=message,
        created_at=datetime.now(timezone.utc),
    )


class _ModelCalls:
    def __init__(self) -> None:
        self.context = None
        self.completed = None

    def prepare_session_call(self, *, context, mapper, **kwargs):
        del mapper, kwargs
        self.context = context
        prepared = PreparedModelCall(
            provider="worker",
            api_kind="test",
            endpoint="test",
            requested_model="worker-model",
            body={"stream": True},
        )
        return SimpleNamespace(model_call=SimpleNamespace(), prepared=prepared)

    def complete_session_response(self, *, call, response):
        self.completed = (call, response)


class _SendGate:
    async def send(self, *, call, prepared, effects):
        del call, prepared, effects
        return ModelCallResponse(
            assistant_message=AssistantMessage((TextBlock("压缩摘要"),)),
            provider_response={"ok": True},
            started_at=datetime.now(timezone.utc),
            first_chunk_at=None,
            finished_at=datetime.now(timezone.utc),
            http_status=200,
        )


def test_model_backed_generator_keeps_recent_tail_and_persists_worker_response():
    model_calls = _ModelCalls()

    async def run():
        generator = ModelBackedHistoryCompactionGenerator(
            model_calls=model_calls,
            send_gate=_SendGate(),
            preserve_tail_tokens=1,
            summary_input_tokens=100,
        )
        nodes = (
            _node("user-1", UserMessage((TextBlock("old request"),))),
            _node("assistant-1", AssistantMessage((TextBlock("old answer"),))),
            _node("user-2", UserMessage((TextBlock("recent request"),))),
            _node("assistant-2", AssistantMessage((TextBlock("recent answer"),))),
        )

        return await generator.generate(
            session_id="session-1",
            nodes=nodes,
            model_context=ModelContext(system=SystemContent(), messages=()),
            preflight=TokenPreflightResult(
                token_count=100,
                threshold=90,
                compaction_required=True,
                source="estimated",
            ),
            runtime_effects=RuntimeEffects(provider=object(), worker_provider=object()),
        )

    result = asyncio.run(run())

    assert result.summary == "压缩摘要"
    assert result.first_kept_node_id == "user-2"
    assert (
        model_calls.context.messages[0].content[0].text.startswith("请压缩以下历史消息")
    )
    assert model_calls.completed is not None


def test_model_backed_generator_requires_worker_provider():
    generator = ModelBackedHistoryCompactionGenerator(
        model_calls=_ModelCalls(), send_gate=_SendGate()
    )

    async def run():
        return await generator.generate(
            session_id="session-1",
            nodes=(_node("user-1", UserMessage()), _node("user-2", UserMessage())),
            model_context=ModelContext(system=SystemContent(), messages=()),
            preflight=TokenPreflightResult(1, 0, True, "estimated"),
            runtime_effects=RuntimeEffects(provider=object()),
        )

    try:
        asyncio.run(run())
    except RuntimeError as exc:
        assert "需要配置 worker model" in str(exc)
    else:
        raise AssertionError("缺少 worker provider 时必须失败")
