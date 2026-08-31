import asyncio

import pytest

from pickel.context.history_compaction import HistoryCompactionError
from pickel.context.model_context import ModelContext
from pickel.conversations.agent_message import (
    AssistantMessage,
    ToolResultMessage,
    UserMessage,
)
from pickel.conversations.content_blocks import TextBlock, ToolCallBlock
from pickel.runtime.history_compaction_worker import (
    ModelBackedHistoryCompactionGenerator,
)


class _Sender:
    def __init__(self, text: str = "摘要") -> None:
        self.calls: list[tuple[ModelContext, str]] = []
        self.text = text

    async def __call__(self, *, context: ModelContext, purpose: str):
        self.calls.append((context, purpose))
        return AssistantMessage((TextBlock(self.text),))


def _run(messages, sender=None, **kwargs):
    sender = sender or _Sender()
    result = asyncio.run(
        ModelBackedHistoryCompactionGenerator().generate(
            previous_summary=kwargs.pop("previous_summary", None),
            exact_messages=messages,
            previous_read_files=kwargs.pop("previous_read_files", ()),
            previous_modified_files=kwargs.pop("previous_modified_files", ()),
            worker_input_limit=kwargs.pop("worker_input_limit", 10_000),
            send_summarizer=sender,
            max_summary_tokens=kwargs.pop("max_summary_tokens", 4096),
            preserve_tail_tokens=kwargs.pop("preserve_tail_tokens", 1),
        )
    )
    assert not kwargs
    return result, sender


def test_generator_is_provider_neutral_and_keeps_exact_tail():
    messages = (
        UserMessage((TextBlock("old request"),)),
        AssistantMessage((TextBlock("old answer"),)),
        UserMessage((TextBlock("recent request"),)),
        AssistantMessage((TextBlock("recent answer"),)),
    )
    result, sender = _run(messages)

    assert result.summary == "摘要"
    assert result.retained_messages == messages[-2:]
    assert "recent request" not in sender.calls[0][0].messages[0].content[0].text
    assert sender.calls[0][0].tools == ()
    assert sender.calls[0][1] == "history_compaction"


def test_previous_summary_enters_worker_input_but_not_retained_tail():
    messages = (
        UserMessage((TextBlock("old"),)),
        AssistantMessage((TextBlock("old answer"),)),
        UserMessage((TextBlock("new"),)),
        AssistantMessage((TextBlock("new answer"),)),
    )
    result, sender = _run(
        messages,
        previous_summary="previous checkpoint",
        previous_read_files=("a.py",),
        previous_modified_files=("b.py",),
    )

    prompt = sender.calls[0][0].messages[0].content[0].text
    assert "previous checkpoint" in prompt
    assert "a.py" in prompt and "b.py" in prompt
    assert result.retained_messages == messages[-2:]


def test_tool_result_tail_is_never_orphaned():
    call = AssistantMessage((ToolCallBlock("call-1", "read", {"path": "src/a.py"}),))
    result_message = ToolResultMessage("call-1", "read", (TextBlock("ok"),))
    messages = (
        UserMessage((TextBlock("old"),)),
        AssistantMessage((TextBlock("old answer"),)),
        call,
        result_message,
        UserMessage((TextBlock("recent"),)),
    )
    result, sender = _run(
        messages,
        preserve_tail_tokens=(
            ModelBackedHistoryCompactionGenerator._message_cost(result_message)
            + ModelBackedHistoryCompactionGenerator._message_cost(messages[-1])
        ),
    )

    assert result.retained_messages[0] == call
    assert result.retained_messages[1] == result_message
    assert "call-1" not in sender.calls[0][0].messages[0].content[0].text
    assert result.read_files == ()


def test_orphan_tool_result_is_a_compaction_failure():
    with pytest.raises(HistoryCompactionError) as exc_info:
        _run(
            (
                UserMessage((TextBlock("old"),)),
                ToolResultMessage("missing", "read"),
                UserMessage((TextBlock("recent"),)),
            )
        )
    assert exc_info.value.code == "history_compaction_tool_pairing"


def test_complete_worker_input_over_limit_fails_without_truncating():
    with pytest.raises(HistoryCompactionError) as exc_info:
        _run(
            (
                UserMessage((TextBlock("x" * 200),)),
                AssistantMessage((TextBlock("answer"),)),
                UserMessage((TextBlock("tail"),)),
            ),
            worker_input_limit=1,
        )
    assert exc_info.value.code == "history_compaction_input_too_large"


def test_tool_result_text_is_sent_in_full():
    call = AssistantMessage((ToolCallBlock("c", "bash", {"command": "cat"}),))
    result_message = ToolResultMessage("c", "bash", (TextBlock("z" * 5000),))
    _, sender = _run(
        (
            UserMessage((TextBlock("old"),)),
            call,
            result_message,
            UserMessage((TextBlock("tail"),)),
        ),
        preserve_tail_tokens=1,
    )
    prompt = sender.calls[0][0].messages[0].content[0].text
    assert "z" * 5000 in prompt
    assert "中间内容已截断" not in prompt


def test_system_declares_exactly_nine_sections():
    _, sender = _run(
        (
            UserMessage((TextBlock("old"),)),
            AssistantMessage((TextBlock("answer"),)),
            UserMessage((TextBlock("tail"),)),
        )
    )
    text = sender.calls[0][0].system.sections[0].text
    headings = [line for line in text.splitlines() if line.startswith("## ")]
    assert headings == [
        "## 当前目标与用户意图",
        "## 已确认约束与偏好",
        "## 关键决策与理由",
        "## 已完成工作与当前状态",
        "## 文件与代码",
        "## 已验证命令与结果",
        "## 错误、失败尝试与修复",
        "## 未完成事项与开放问题",
        "## 下一步",
    ]
