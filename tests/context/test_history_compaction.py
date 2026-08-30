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
from pickel.conversations.conversation_node import (
    ConversationNode,
    HistoryCompaction,
)
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
        summary_input_tokens=100,
    )

    result = asyncio.run(
        generator.generate(
            nodes=_nodes(),
            model_context=ModelContext(system=SystemContent(), messages=()),
            preflight=_preflight(),
            send_summarizer=sender,
            max_summary_tokens=4096,
            preserve_tail_tokens=1,
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
                max_summary_tokens=4096,
                preserve_tail_tokens=1,
            )
        )
    assert exc_info.value.code == "history_compaction_empty"


def test_model_backed_generator_does_not_swallow_sender_failures():
    """发送失败原样上抛；降级语义由 OperationDriver 决定，Generator 不吞错。"""
    generator = ModelBackedHistoryCompactionGenerator(
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
                max_summary_tokens=4096,
                preserve_tail_tokens=1,
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
        summary_input_tokens=100,
    )
    result = asyncio.run(
        generator.generate(
            nodes=nodes,
            model_context=ModelContext(system=SystemContent(), messages=()),
            preflight=_preflight(),
            send_summarizer=sender,
            max_summary_tokens=4096,
            preserve_tail_tokens=budget,
        )
    )

    # first_kept 是 call 节点而非 result 节点；result 连同 call 一起保留。
    assert result.first_kept_node_id == "assistant-call"
    rendered = sender.calls[0][0].messages[0].content[0].text
    assert "call-1" not in rendered  # 被保留的配对不进入摘要输入


def test_summary_request_uses_structured_checkpoint_skeleton():
    """摘要请求的 system 段必须是固定骨架；缺节不失败，但骨架必须声明。"""
    sender = _FakeSender()
    generator = ModelBackedHistoryCompactionGenerator(summary_input_tokens=100)

    asyncio.run(
        generator.generate(
            nodes=_nodes(),
            model_context=ModelContext(system=SystemContent(), messages=()),
            preflight=_preflight(),
            send_summarizer=sender,
            max_summary_tokens=4096,
            preserve_tail_tokens=1,
        )
    )

    context, _ = sender.calls[0]
    section = context.system.sections[0]
    assert section.name == "history_compaction"
    for heading in (
        "## 目标与意图",
        "## 关键决策",
        "## 已验证的命令与结果",
        "## 文件与代码",
        "## 错误与修复",
        "## 未完成事项",
        "## 当前进展",
        "## 下一步",
        "## 关键上下文",
    ):
        assert heading in section.text


def test_oversized_tool_result_is_truncated_only_in_summary_input():
    """超限 tool result 渲染为 head+标记+tail；历史节点本身不被改写。"""
    long_output = "x" * 5000
    sender = _FakeSender()
    generator = ModelBackedHistoryCompactionGenerator(summary_input_tokens=2000)
    nodes = (
        _node("user-1", UserMessage((TextBlock("old request"),))),
        _node("assistant-1", AssistantMessage((TextBlock("old answer"),))),
        ConversationNode(
            node_id="tool-result",
            session_id="session-1",
            parent_node_id=None,
            content_type="agent_message",
            content=ToolResultMessage(
                tool_call_id="call-1",
                tool_name="bash",
                content=(TextBlock(long_output),),
            ),
            created_at=datetime.now(timezone.utc),
        ),
        _node("user-2", UserMessage((TextBlock("recent request"),))),
        _node("assistant-2", AssistantMessage((TextBlock("recent answer"),))),
    )
    asyncio.run(
        generator.generate(
            nodes=nodes,
            model_context=ModelContext(system=SystemContent(), messages=()),
            preflight=_preflight(),
            send_summarizer=sender,
            max_summary_tokens=4096,
            preserve_tail_tokens=1,
        )
    )

    rendered = sender.calls[0][0].messages[0].content[0].text
    assert "[... 中间内容已截断 ...]" in rendered
    assert "x" * 2000 not in rendered
    # 历史节点未被改写：原始超限文本仍在投影节点里。
    assert nodes[2].content.content[0].text == long_output


def test_summary_must_shrink_the_shadowed_region():
    """摘要不小于被压缩区域时压缩无效，按 no_shrink 失败并走降级。"""
    sender = _FakeSender(
        message=AssistantMessage((TextBlock("y" * 8000),)),
    )
    generator = ModelBackedHistoryCompactionGenerator(summary_input_tokens=100)

    with pytest.raises(HistoryCompactionError) as exc_info:
        asyncio.run(
            generator.generate(
                nodes=_nodes(),
                model_context=ModelContext(system=SystemContent(), messages=()),
                preflight=_preflight(),
                send_summarizer=sender,
                max_summary_tokens=4096,
                preserve_tail_tokens=1,
            )
        )
    assert exc_info.value.code == "history_compaction_no_shrink"


def test_summary_respects_frozen_output_budget():
    """摘要超过冻结的输出预算即失败，防止无效压缩白付调用成本。"""
    sender = _FakeSender(
        message=AssistantMessage((TextBlock("摘要" * 10),)),
    )
    generator = ModelBackedHistoryCompactionGenerator(summary_input_tokens=100)

    with pytest.raises(HistoryCompactionError) as exc_info:
        asyncio.run(
            generator.generate(
                nodes=_nodes(),
                model_context=ModelContext(system=SystemContent(), messages=()),
                preflight=_preflight(),
                send_summarizer=sender,
                max_summary_tokens=1,
                preserve_tail_tokens=1,
            )
        )
    assert exc_info.value.code == "history_compaction_summary_too_long"


def test_file_ledger_accumulates_across_compactions():
    """账本 = 被压缩区域的内置读写调用 + 前序压缩节点账本的并集。"""
    nodes = (
        _node("user-old", UserMessage((TextBlock("old"),))),
        _node(
            "assistant-calls",
            AssistantMessage(
                (
                    ToolCallBlock("c1", "read", {"path": "src/a.py"}),
                    ToolCallBlock("c2", "edit", {"path": "src/b.py"}),
                )
            ),
        ),
        ConversationNode(
            node_id="result-1",
            session_id="session-1",
            parent_node_id=None,
            content_type="agent_message",
            content=ToolResultMessage(tool_call_id="c1", tool_name="read"),
            created_at=datetime.now(timezone.utc),
        ),
        ConversationNode(
            node_id="result-2",
            session_id="session-1",
            parent_node_id=None,
            content_type="agent_message",
            content=ToolResultMessage(tool_call_id="c2", tool_name="edit"),
            created_at=datetime.now(timezone.utc),
        ),
        ConversationNode(
            node_id="compaction-1",
            session_id="session-1",
            parent_node_id=None,
            content_type="history_compaction",
            content=HistoryCompaction(
                "更早的摘要",
                "user-old",
                ("docs/d.md",),
                ("docs/e.md",),
            ),
            created_at=datetime.now(timezone.utc),
        ),
        _node("user-recent", UserMessage((TextBlock("recent"),))),
        _node("assistant-recent", AssistantMessage((TextBlock("recent answer"),))),
    )
    sender = _FakeSender()
    generator = ModelBackedHistoryCompactionGenerator(summary_input_tokens=2000)

    result = asyncio.run(
        generator.generate(
            nodes=nodes,
            model_context=ModelContext(system=SystemContent(), messages=()),
            preflight=_preflight(),
            send_summarizer=sender,
            max_summary_tokens=4096,
            preserve_tail_tokens=1,
        )
    )

    assert result.read_files == ("docs/d.md", "src/a.py")
    assert result.modified_files == ("docs/e.md", "src/b.py")
    assert result.first_kept_node_id == "user-recent"
    # 前序压缩账本随 payload 一起回喂摘要 worker。
    rendered = sender.calls[0][0].messages[0].content[0].text
    assert "docs/d.md" in rendered
