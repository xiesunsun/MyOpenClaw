import asyncio
import json
from datetime import datetime, timezone

import pytest

from pickel.context.history_compaction import HistoryCompactionError
from pickel.context.model_context import ModelContext, SystemContent
from pickel.context.token_preflight import TokenPreflightResult
from pickel.conversations.agent_message import (
    AssistantMessage,
    ToolResultMessage,
    UserMessage,
    agent_message_to_dict,
)
from pickel.conversations.content_blocks import TextBlock, ToolCallBlock
from pickel.conversations.conversation_node import ConversationNode
from pickel.runtime.history_compaction_worker import (
    ModelBackedHistoryCompactionGenerator,
)
from pickel.runtime.worker_call_sender import WorkerCallSendError


def _node(node_id: str, message) -> ConversationNode:
    return ConversationNode(
        node_id=node_id,
        session_id="session-1",
        parent_node_id=None,
        content_type="agent_message",
        content=message,
        created_at=datetime.now(timezone.utc),
    )


def _nodes() -> tuple[ConversationNode, ...]:
    return (
        _node("user-1", UserMessage((TextBlock("old request"),))),
        _node("assistant-1", AssistantMessage((TextBlock("old answer"),))),
        _node("user-2", UserMessage((TextBlock("recent request"),))),
        _node("assistant-2", AssistantMessage((TextBlock("recent answer"),))),
    )


def _preflight() -> TokenPreflightResult:
    return TokenPreflightResult(
        token_count=100,
        threshold=90,
        compaction_required=True,
        source="estimated",
    )


class _FakeSender:
    """记录摘要输入、返回固定摘要的窄 SummarizerSender fake。"""

    def __init__(
        self,
        message: AssistantMessage | None = None,
        error: Exception | None = None,
    ) -> None:
        self.calls: list[tuple[ModelContext, str]] = []
        self._message = message or AssistantMessage((TextBlock("压缩摘要"),))
        self._error = error

    async def __call__(self, *, context: ModelContext, purpose: str):
        self.calls.append((context, purpose))
        if self._error is not None:
            raise self._error
        return self._message


def test_model_backed_generator_keeps_recent_tail_and_sends_summary_request():
    sender = _FakeSender()
    generator = ModelBackedHistoryCompactionGenerator(
        preserve_tail_tokens=1,
        summary_input_tokens=100,
    )

    result = asyncio.run(
        generator.generate(
            nodes=_nodes(),
            model_context=ModelContext(system=SystemContent(), messages=()),
            preflight=_preflight(),
            send_summarizer=sender,
        )
    )

    assert result.summary == "压缩摘要"
    assert result.first_kept_node_id == "user-2"
    assert len(sender.calls) == 1
    context, purpose = sender.calls[0]
    assert purpose == "history_compaction"
    assert context.tools == ()
    assert context.messages[0].content[0].text.startswith("请压缩以下历史消息")
    # 摘要输入只包含被压缩的旧历史，不含保留尾部。
    assert "recent request" not in context.messages[0].content[0].text


def test_model_backed_generator_rejects_empty_summary():
    generator = ModelBackedHistoryCompactionGenerator(
        preserve_tail_tokens=1,
        summary_input_tokens=100,
    )

    with pytest.raises(HistoryCompactionError) as exc_info:
        asyncio.run(
            generator.generate(
                nodes=_nodes(),
                model_context=ModelContext(system=SystemContent(), messages=()),
                preflight=_preflight(),
                send_summarizer=_FakeSender(
                    message=AssistantMessage((TextBlock("   "),))
                ),
            )
        )
    assert exc_info.value.code == "history_compaction_empty"


def test_model_backed_generator_does_not_swallow_sender_failures():
    """发送失败原样上抛；降级语义由 OperationDriver 决定，Generator 不吞错。"""
    generator = ModelBackedHistoryCompactionGenerator(
        preserve_tail_tokens=1,
        summary_input_tokens=100,
    )
    sender = _FakeSender(error=WorkerCallSendError("worker 调用失败"))

    with pytest.raises(WorkerCallSendError):
        asyncio.run(
            generator.generate(
                nodes=_nodes(),
                model_context=ModelContext(system=SystemContent(), messages=()),
                preflight=_preflight(),
                send_summarizer=sender,
            )
        )


def _cost(node: ConversationNode) -> int:
    """与生成器相同的节点成本估算口径。"""
    return max(1, len(json.dumps(agent_message_to_dict(node.content))) // 4)


def test_tail_cut_never_separates_tool_result_from_its_call():
    """贪心切点落在 ToolResult 上时，必须下修到它的 ToolCall 所在节点。"""
    nodes = (
        _node("user-oldest", UserMessage((TextBlock("old"),))),
        _node("assistant-older", AssistantMessage((TextBlock("even older"),))),
        _node(
            "assistant-call",
            AssistantMessage(
                (
                    TextBlock("checking"),
                    ToolCallBlock("call-1", "bash", {"command": "ls -la " + "x" * 500}),
                )
            ),
        ),
        ConversationNode(
            node_id="tool-result",
            session_id="session-1",
            parent_node_id=None,
            content_type="agent_message",
            content=ToolResultMessage(tool_call_id="call-1", tool_name="bash"),
            created_at=datetime.now(timezone.utc),
        ),
        _node("user-recent", UserMessage((TextBlock("recent"),))),
        _node("assistant-recent", AssistantMessage((TextBlock("recent answer"),))),
    )
    budget = _cost(nodes[5]) + _cost(nodes[4]) + _cost(nodes[3])
    assert _cost(nodes[2]) > budget  # 前置：无修复时贪心切点恰好落在 result 上

    sender = _FakeSender()
    generator = ModelBackedHistoryCompactionGenerator(
        preserve_tail_tokens=budget,
        summary_input_tokens=100,
    )
    result = asyncio.run(
        generator.generate(
            nodes=nodes,
            model_context=ModelContext(system=SystemContent(), messages=()),
            preflight=_preflight(),
            send_summarizer=sender,
        )
    )

    # first_kept 是 call 节点而非 result 节点；result 连同 call 一起保留。
    assert result.first_kept_node_id == "assistant-call"
    rendered = sender.calls[0][0].messages[0].content[0].text
    assert "call-1" not in rendered  # 被保留的配对不进入摘要输入
