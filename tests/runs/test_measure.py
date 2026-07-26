"""measure：total 三档 + 本地分栏归一化（设计 §6.1 / §6.2）。"""

from __future__ import annotations

import asyncio

from pickel.context.model_context import (
    ModelContext,
    SystemContent,
    SystemSection,
    ToolDefinition,
)
from pickel.conversations.agent_message import AssistantMessage, UserMessage
from pickel.conversations.content_blocks import TextContent
from pickel.runs.measure import measure
from pickel.runs.usage_anchor import UsageAnchor
from pickel.shared.model_config import ModelConfig


class _StubProvider:
    def __init__(self, result: int | None = 5000) -> None:
        self._result = result
        self.calls: list[ModelContext] = []

    async def count_context_tokens(self, context: ModelContext) -> int | None:
        self.calls.append(context)
        return self._result


def _model_config(max_input_tokens: int | None = 200_000) -> ModelConfig:
    return ModelConfig(
        provider="anthropic",
        model="claude-sonnet-5",
        max_input_tokens=max_input_tokens,
    )


def _request(*, with_messages: bool = True) -> ModelContext:
    return ModelContext(
        system=SystemContent(
            sections=[
                SystemSection(name="behavior", text="b" * 400),
                SystemSection(name="skills_guidance", text="g" * 200),
                SystemSection(name="skills_catalog", text="- alpha: does A\n- beta: does B"),
            ]
        ),
        messages=(
            [
                UserMessage(content=[TextContent(text="u" * 800)]),
                AssistantMessage(content=[TextContent(text="a" * 400)]),
            ]
            if with_messages
            else []
        ),
        tools=[
            ToolDefinition(
                name="echo",
                description="Echo text",
                input_schema={"type": "object", "properties": {"text": {"type": "string"}}},
            )
        ],
    )


def _measure(*, request=None, anchor=None, provider=None, max_input_tokens=200_000):
    return asyncio.run(
        measure(
            request=request if request is not None else _request(),
            anchor=anchor,
            provider=provider if provider is not None else _StubProvider(),
            model_config=_model_config(max_input_tokens),
        )
    )


def test_档A_锚命中时不调用_provider():
    provider = _StubProvider()
    anchor = UsageAnchor(input_tokens=9000, output_tokens=100, trailing_messages=[])

    usage = _measure(anchor=anchor, provider=provider)

    assert provider.calls == []
    assert usage.total_tokens == 9100
    assert usage.total_source == "anchor"


def test_档B_锚加尾部估计时不调用_provider():
    provider = _StubProvider()
    trailing = [UserMessage(content=[TextContent(text="t" * 400)])]
    anchor = UsageAnchor(input_tokens=9000, output_tokens=100, trailing_messages=trailing)

    usage = _measure(anchor=anchor, provider=provider)

    assert provider.calls == []
    assert usage.total_tokens > 9100
    assert usage.total_source == "anchor_plus_tail"


def test_档C_无锚时恰好调用一次_provider():
    provider = _StubProvider(result=5000)

    usage = _measure(anchor=None, provider=provider)

    assert len(provider.calls) == 1
    assert usage.total_tokens == 5000
    assert usage.total_source == "counted"


def test_档C_provider_返回_None_时降级为本地估计():
    provider = _StubProvider(result=None)

    usage = _measure(anchor=None, provider=provider)

    assert len(provider.calls) == 1
    assert usage.total_source == "estimated"
    assert usage.total_tokens > 0


def test_空会话不调用_provider():
    """Anthropic count_tokens 要求 messages 非空，空会话必须走本地估计。"""
    provider = _StubProvider()

    usage = _measure(request=_request(with_messages=False), anchor=None, provider=provider)

    assert provider.calls == []
    assert usage.total_source == "estimated"
    assert usage.total_tokens > 0
    assert usage.category("messages").tokens == 0


def test_分栏之和恒等于_total():
    for anchor in (
        None,
        UsageAnchor(input_tokens=9000, output_tokens=100, trailing_messages=[]),
        UsageAnchor(
            input_tokens=50,
            output_tokens=1,
            trailing_messages=[UserMessage(content=[TextContent(text="x" * 40)])],
        ),
    ):
        usage = _measure(anchor=anchor)

        assert sum(c.tokens for c in usage.categories) == usage.total_tokens


def test_所有栏位非负_即使_total_远小于原始估计():
    """scale 远小于 1 时不得出现负值。"""
    anchor = UsageAnchor(input_tokens=10, output_tokens=1, trailing_messages=[])

    usage = _measure(anchor=anchor)

    assert usage.total_tokens == 11
    assert all(c.tokens >= 0 for c in usage.categories)
    assert sum(c.tokens for c in usage.categories) == 11


def test_分栏覆盖三段_system_与_messages_tools_other():
    usage = _measure(anchor=None)

    assert [c.key for c in usage.categories] == [
        "behavior",
        "skills_guidance",
        "skills_catalog",
        "messages",
        "tools",
        "other",
    ]


def test_skills_明细逐条拆分且不触网():
    provider = _StubProvider()

    usage = _measure(anchor=None, provider=provider)

    details = usage.category("skills_catalog").details
    assert [d.label for d in details] == ["- alpha: does A", "- beta: does B"]
    assert len(provider.calls) == 1  # 明细未额外触发远程差分


def test_free_与_max_口径():
    anchor = UsageAnchor(input_tokens=9000, output_tokens=100, trailing_messages=[])

    usage = _measure(anchor=anchor, max_input_tokens=200_000)
    assert usage.free_tokens == 200_000 - 9100

    unknown = _measure(anchor=anchor, max_input_tokens=None)
    assert unknown.free_tokens is None


def test_model_label():
    usage = _measure(anchor=None)

    assert usage.model_label == "anthropic / claude-sonnet-5"
