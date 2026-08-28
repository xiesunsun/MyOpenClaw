from __future__ import annotations

import asyncio

import pytest

from pickel.context.model_context import ModelContext, SystemContent
from pickel.context.token_preflight import (
    ContextCompactionRequired,
    preflight_model_context,
)


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


def test_provider_count_is_preferred_and_capacity_reserves_safety_margin() -> None:
    provider = _Provider(value=89)

    result = asyncio.run(
        preflight_model_context(
            context=_context(),
            provider=provider,
            effective_input_capacity=100,
            safety_margin_tokens=10,
        )
    )

    assert result.source == "provider"
    assert result.token_count == 89
    assert result.effective_input_capacity == 100
    assert result.threshold == 90
    assert result.compaction_required is False
    assert provider.contexts == [_context()]


def test_threshold_is_inclusive_and_raises_only_compaction_signal() -> None:
    with pytest.raises(ContextCompactionRequired) as caught:
        asyncio.run(
            preflight_model_context(
                context=_context(),
                provider=_Provider(value=90),
                effective_input_capacity=100,
                safety_margin_tokens=10,
            )
        )

    assert caught.value.result.source == "provider"
    assert caught.value.result.compaction_required is True
    assert caught.value.result.threshold == 90
    assert "compaction required" in str(caught.value).lower()


def test_provider_unavailable_uses_explicit_conservative_estimate() -> None:
    provider = _Provider(value=None)
    result = asyncio.run(
        preflight_model_context(
            context=_context(),
            provider=provider,
            effective_input_capacity=10_000,
            safety_margin_tokens=100,
        )
    )

    assert result.source == "conservative_estimate"
    assert result.token_count == result.estimated_upper_bound_tokens
    assert result.token_count > 0
    assert result.compaction_required is False


def test_provider_count_error_falls_back_without_hiding_estimate_source() -> None:
    result = asyncio.run(
        preflight_model_context(
            context=_context(),
            provider=_Provider(error=RuntimeError("count failed")),
            effective_input_capacity=None,
        )
    )

    assert result.source == "conservative_estimate"
    assert result.effective_input_capacity is None
    assert result.threshold is None
    assert result.compaction_required is False


def test_zero_effective_capacity_is_a_known_compaction_boundary() -> None:
    with pytest.raises(ContextCompactionRequired) as caught:
        asyncio.run(
            preflight_model_context(
                context=_context(),
                provider=_Provider(value=0),
                effective_input_capacity=0,
                safety_margin_tokens=0,
            )
        )

    assert caught.value.result.threshold == 0
