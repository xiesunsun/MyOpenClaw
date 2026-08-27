"""Provider 基类只暴露 PreparedModelCall 发送合同。"""

from __future__ import annotations

import asyncio

import pytest

from pickel.context.model_context import ModelContext, SystemContent
from pickel.conversations.agent_message import AssistantMessage
from pickel.conversations.content_blocks import TextBlock
from pickel.model_calls.prepared import PreparedModelCall
from pickel.providers.base import Provider
from pickel.providers.stream import StreamCompleted, accumulate
from pickel.shared.model_config import ModelConfig


class _OnlyPreparedProvider(Provider):
    def __init__(self) -> None:
        self.calls = 0

    @classmethod
    def from_config(cls, config: ModelConfig) -> "_OnlyPreparedProvider":
        raise NotImplementedError

    def prepare(self, context: ModelContext) -> PreparedModelCall:
        del context
        return PreparedModelCall(
            provider="test",
            api_kind="test",
            endpoint="generate",
            requested_model="test",
            body={"stream": True},
        )

    async def stream_prepared(self, prepared: PreparedModelCall):
        assert prepared.body["stream"] is True
        self.calls += 1
        yield StreamCompleted(AssistantMessage((TextBlock("done"),)))


class _NoSenderProvider(_OnlyPreparedProvider):
    stream_prepared = Provider.stream_prepared


def _context() -> ModelContext:
    return ModelContext(system=SystemContent.from_text("sys"), messages=[], tools=[])


def test_prepared_stream_can_be_accumulated() -> None:
    provider = _OnlyPreparedProvider()
    result = asyncio.run(
        accumulate(provider.stream_prepared(provider.prepare(_context())))
    )
    assert result.content[0].text == "done"
    assert provider.calls == 1


def test_provider_base_does_not_expose_legacy_generation_entries() -> None:
    assert not hasattr(Provider, "generate")
    assert not hasattr(Provider, "stream")


def test_missing_prepared_sender_is_explicitly_unsupported() -> None:
    with pytest.raises(NotImplementedError):
        asyncio.run(
            accumulate(
                _NoSenderProvider().stream_prepared(
                    PreparedModelCall(
                        provider="test",
                        api_kind="test",
                        endpoint="generate",
                        requested_model="test",
                        body={},
                    )
                )
            )
        )
