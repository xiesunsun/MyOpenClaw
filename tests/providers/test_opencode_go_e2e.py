"""OpenCode Go 三种 wire 的可选端到端烟测。"""

from __future__ import annotations

import asyncio
import os

import pytest

from pickel.context.model_context import ModelContext, SystemContent
from pickel.conversations.agent_message import UserMessage
from pickel.conversations.content_blocks import TextBlock
from pickel.providers.anthropic import AnthropicMessagesProvider
from pickel.providers.openai import OpenAIResponsesProvider
from pickel.providers.openai_chat_completions import OpenAIChatCompletionsProvider
from pickel.providers.stream import accumulate

pytestmark = pytest.mark.skipif(
    not os.environ.get("OPENCODE_GO_API_KEY"),
    reason="需要 OPENCODE_GO_API_KEY",
)

_API_BASE = "https://opencode.ai/zen/go/v1"


def _context() -> ModelContext:
    return ModelContext(
        system=SystemContent.from_text("只输出 OPENCODE_GO_OK。"),
        messages=(UserMessage((TextBlock("开始"),)),),
    )


@pytest.mark.parametrize(
    ("provider_type", "model", "provider_options", "max_output_tokens"),
    [
        (OpenAIResponsesProvider, "gpt-5.6-luna", {}, 64),
        (
            OpenAIChatCompletionsProvider,
            "deepseek-v4-flash",
            {},
            64,
        ),
        (
            OpenAIChatCompletionsProvider,
            "glm-5.3-flash",
            {
                "tool_stream": True,
                "preserve_thinking": True,
                "thinking": {"type": "enabled", "clear_thinking": False},
                "reasoning_effort": "max",
            },
            512,
        ),
        (AnthropicMessagesProvider, "minimax-m3", {}, 64),
    ],
)
def test_opencode_go_text_stream(
    provider_type, model: str, provider_options, max_output_tokens: int
) -> None:
    async def run():
        provider = provider_type(
            model=model,
            provider_name="opencode-go",
            api_base=_API_BASE,
            api_key=os.environ["OPENCODE_GO_API_KEY"],
            max_output_tokens=max_output_tokens,
            provider_options={"timeout_seconds": 90, **provider_options},
        )
        try:
            return await accumulate(
                provider.stream_prepared(provider.prepare(_context()))
            )
        finally:
            client = getattr(provider, "client", None)
            if client is not None and hasattr(client, "aclose"):
                await client.aclose()

    message = asyncio.run(run())
    text = "".join(
        block.text for block in message.content if isinstance(block, TextBlock)
    )

    assert "OPENCODE_GO_OK" in text
    assert message.metadata is not None
    assert message.metadata.provider == "opencode-go"
