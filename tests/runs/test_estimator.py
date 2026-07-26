"""本地 token 估计器：纯计算、无网络。"""

from __future__ import annotations

from pathlib import Path

import pickel.runs.estimator as estimator_module
from pickel.context.model_context import ToolDefinition
from pickel.conversations.agent_message import (
    AssistantMessage,
    ToolResultMessage,
    UserMessage,
)
from pickel.conversations.content_blocks import (
    ImageContent,
    TextContent,
    ThinkingContent,
    ToolCallContent,
)
from pickel.runs.estimator import estimate_messages, estimate_text, estimate_tools


def test_estimate_text_scales_with_length():
    assert estimate_text("") == 0
    assert estimate_text("abcd") == 1
    assert estimate_text("a" * 400) == 100


def test_estimate_text_never_negative_or_none():
    assert estimate_text("a") >= 0


def test_estimate_messages_counts_all_block_kinds():
    messages = [
        UserMessage(content=[TextContent(text="a" * 40)]),
        AssistantMessage(
            content=[
                ThinkingContent(text="b" * 40),
                ToolCallContent(id="c1", name="echo", arguments={"text": "c" * 40}),
            ]
        ),
        ToolResultMessage(
            tool_call_id="c1",
            tool_name="echo",
            content=[TextContent(text="d" * 40)],
        ),
    ]
    total = estimate_messages(messages)

    # 四类内容都要计入，故明显大于单条消息的估计
    assert total > estimate_messages(messages[:1]) * 3


def test_estimate_messages_empty_is_zero():
    assert estimate_messages([]) == 0


def test_estimate_messages_handles_image_without_text():
    messages = [UserMessage(content=[ImageContent(media_type="image/png", url="x")])]

    # 不抛异常；图片按固定成本计入，不为负
    assert estimate_messages(messages) >= 0


def test_estimate_tools_counts_name_description_and_schema():
    tools = [
        ToolDefinition(
            name="echo",
            description="Echo text back",
            input_schema={"type": "object", "properties": {"text": {"type": "string"}}},
        )
    ]
    assert estimate_tools(tools) > 0
    assert estimate_tools([]) == 0


def test_estimator_module_has_no_provider_or_async_dependency():
    """§6.2 强制：分栏估计不得触网，故源码不得引入 provider 或异步。"""
    source = Path(estimator_module.__file__).read_text(encoding="utf-8")

    assert "pickel.providers" not in source
    assert "async def" not in source
    assert "await " not in source
