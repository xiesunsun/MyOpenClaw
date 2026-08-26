"""CPA 上 gpt-5.6-luna 的 Responses API 端到端合同。"""

from __future__ import annotations

import asyncio
import os

import pytest

from pickel.context.model_context import ModelContext, SystemContent, ToolDefinition
from pickel.conversations.agent_message import (
    AssistantMessage,
    ToolResultMessage,
    UserMessage,
)
from pickel.conversations.content_blocks import TextBlock, ToolCallBlock
from pickel.providers.openai import OpenAIProvider

pytestmark = pytest.mark.skipif(
    not os.environ.get("CPA_BASE_URL") or not os.environ.get("CPA_API_KEY"),
    reason="需要 CPA_BASE_URL 和 CPA_API_KEY",
)


def test_cpa_responses_streaming_tool_round_trip() -> None:
    provider = OpenAIProvider(
        model="gpt-5.6-luna",
        api_base=os.environ.get("CPA_BASE_URL"),
        api_key=os.environ.get("CPA_API_KEY"),
        max_output_tokens=128,
        provider_options={"reasoning_effort": "low", "timeout_seconds": 90},
    )
    user = UserMessage(
        (TextBlock("必须调用 lookup_value，参数 key 必须是 pickel。不要直接回答。"),)
    )
    tool = ToolDefinition(
        "lookup_value",
        "按 key 查询值",
        {
            "type": "object",
            "properties": {"key": {"type": "string"}},
            "required": ["key"],
            "additionalProperties": False,
        },
    )

    first = asyncio.run(
        provider.generate(
            ModelContext(
                system=SystemContent.from_text("严格遵循用户的工具调用要求。"),
                messages=(user,),
                tools=(tool,),
            )
        )
    )
    call = next(block for block in first.content if isinstance(block, ToolCallBlock))
    assert call.name == "lookup_value"
    assert call.arguments["key"] == "pickel"

    second = asyncio.run(
        provider.generate(
            ModelContext(
                system=SystemContent.from_text("用工具结果回答。"),
                messages=(
                    user,
                    AssistantMessage((call,)),
                    ToolResultMessage(
                        tool_call_id=call.id,
                        tool_name=call.name,
                        content=(TextBlock("CPA_TOOL_ROUND_TRIP_OK"),),
                    ),
                ),
                tools=(tool,),
            )
        )
    )
    asyncio.run(provider.client.aclose())

    text = "".join(
        block.text for block in second.content if isinstance(block, TextBlock)
    )
    assert "CPA_TOOL_ROUND_TRIP_OK" in text
