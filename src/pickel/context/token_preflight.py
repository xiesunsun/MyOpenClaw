"""ModelContext 请求前的 token 容量检查。"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable
from dataclasses import dataclass
from typing import Any, Literal

from pickel.context.context_usage import estimate_context_usage
from pickel.context.model_context import ModelContext

TokenCountSource = Literal["counted", "anchor", "anchor_plus_tail", "estimated"]


@dataclass(frozen=True)
class TokenPreflightResult:
    """一次最终 ModelContext 的容量检查结果。"""

    token_count: int
    threshold: int | None
    compaction_required: bool
    source: TokenCountSource


class ContextCompactionRequired(RuntimeError):
    """ModelContext 达到阈值；交由 HistoryCompaction 流程处理。"""

    def __init__(self, result: TokenPreflightResult) -> None:
        self.result = result
        super().__init__(
            "ModelContext compaction required: "
            f"{result.token_count} tokens >= threshold {result.threshold} "
            f"(source={result.source})"
        )


async def preflight_model_context(
    *,
    context: ModelContext,
    provider: Any,
    compaction_threshold: int | None,
) -> TokenPreflightResult:
    """检查最终 Context；Provider 不可计数时复用 usage 锚或明确估算。"""
    _validate_threshold(compaction_threshold)
    token_count = await _provider_count(provider, context)
    if token_count is None:
        usage = estimate_context_usage(
            context,
            model_label="",
            max_input_tokens=compaction_threshold,
        )
        token_count = usage.total_tokens
        source: TokenCountSource = usage.total_source
    else:
        source = "counted"
    compaction_required = (
        compaction_threshold is not None and token_count >= compaction_threshold
    )
    result = TokenPreflightResult(
        token_count=token_count,
        threshold=compaction_threshold,
        compaction_required=compaction_required,
        source=source,
    )
    if result.compaction_required:
        raise ContextCompactionRequired(result)
    return result


async def _provider_count(provider: Any, context: ModelContext) -> int | None:
    counter = getattr(provider, "count_context_tokens", None)
    if not callable(counter):
        return None
    try:
        value = counter(context)
        if isinstance(value, Awaitable) or inspect.isawaitable(value):
            value = await value
    except Exception:
        return None
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _validate_threshold(compaction_threshold: int | None) -> None:
    if compaction_threshold is not None and (
        isinstance(compaction_threshold, bool) or compaction_threshold < 0
    ):
        raise ValueError("compaction_threshold 必须为非负整数或 None")
