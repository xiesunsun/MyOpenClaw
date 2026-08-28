from __future__ import annotations

import asyncio

import pytest

from pickel.context.context_usage import model_context_fingerprint
from pickel.context.model_context import ModelContext, SystemContent
from pickel.context.token_preflight import (
    ContextCompactionRequired,
    preflight_model_context,
)
from pickel.conversations.agent_message import (
    AssistantMessage,
    ModelResponseMetadata,
    ModelUsage,
    UserMessage,
)
from pickel.conversations.content_blocks import TextBlock


class _Provider:
    def __init__(self, value=None, error=None):
        self.value = value
        self.error = error
        self.contexts = []

    async def count_context_tokens(self, context):
        self.contexts.append(context)
        if self.error is not None:
            raise self.error
        return self.value


def _context() -> ModelContext:
    return ModelContext(system=SystemContent.from_text("hello"), messages=())


def test_provider_count_is_preferred() -> None:
    provider = _Provider(value=89)

    result = asyncio.run(
        preflight_model_context(
            context=_context(),
            provider=provider,
            compaction_threshold=90,
        )
    )

    assert result.source == "counted"
    assert result.token_count == 89
    assert result.threshold == 90
    assert result.compaction_required is False
    assert provider.contexts == [_context()]


def test_threshold_is_inclusive_and_raises_only_compaction_signal() -> None:
    with pytest.raises(ContextCompactionRequired) as caught:
        asyncio.run(
            preflight_model_context(
                context=_context(),
                provider=_Provider(value=90),
                compaction_threshold=90,
            )
        )

    assert caught.value.result.source == "counted"
    assert caught.value.result.compaction_required is True
    assert caught.value.result.threshold == 90


@pytest.mark.parametrize("value", [None, -1, True, "12"])
def test_unavailable_or_invalid_provider_count_uses_explicit_estimate(value) -> None:
    result = asyncio.run(
        preflight_model_context(
            context=_context(),
            provider=_Provider(value=value),
            compaction_threshold=10_000,
        )
    )

    assert result.source == "estimated"
    assert result.token_count > 0
    assert result.compaction_required is False


def test_provider_count_error_uses_explicit_estimate() -> None:
    result = asyncio.run(
        preflight_model_context(
            context=_context(),
            provider=_Provider(error=RuntimeError("count failed")),
            compaction_threshold=None,
        )
    )

    assert result.source == "estimated"
    assert result.threshold is None


def test_provider_count_unavailable_reuses_matching_usage_anchor() -> None:
    prefix = ModelContext(
        system=SystemContent.from_text("system"),
        messages=(UserMessage(content=(TextBlock("hello"),)),),
    )
    context = ModelContext(
        system=prefix.system,
        messages=(
            *prefix.messages,
            AssistantMessage(
                content=(TextBlock("answer"),),
                metadata=ModelResponseMetadata(
                    provider="opencode-go",
                    model="glm-5.3-flash",
                    usage=ModelUsage(total_tokens=100),
                    context_fingerprint=model_context_fingerprint(prefix),
                    hook_injected_chars=0,
                ),
            ),
        ),
    )

    result = asyncio.run(
        preflight_model_context(
            context=context,
            provider=_Provider(value=None),
            compaction_threshold=101,
        )
    )

    assert result.source == "anchor"
    assert result.token_count == 100
    assert result.compaction_required is False


def test_zero_threshold_is_a_known_compaction_boundary() -> None:
    with pytest.raises(ContextCompactionRequired) as caught:
        asyncio.run(
            preflight_model_context(
                context=_context(),
                provider=_Provider(value=0),
                compaction_threshold=0,
            )
        )

    assert caught.value.result.threshold == 0


def test_negative_or_boolean_threshold_is_rejected() -> None:
    for threshold in (-1, True):
        with pytest.raises(ValueError, match="compaction_threshold"):
            asyncio.run(
                preflight_model_context(
                    context=_context(),
                    provider=_Provider(value=0),
                    compaction_threshold=threshold,
                )
            )
