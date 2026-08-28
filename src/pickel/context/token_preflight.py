"""ModelContext 请求前的 token 容量检查。"""

from __future__ import annotations

import inspect
import json
from collections.abc import Awaitable
from dataclasses import dataclass
from typing import Any, Literal

from pickel.context.model_context import ModelContext

TokenCountSource = Literal["provider", "conservative_estimate"]

# 没有可靠质量阈值前，只在有效输入容量上预留固定安全余量。该余量不改变
# ModelContext，也不承担 HistoryCompaction 的生成或提交。
DEFAULT_SAFETY_MARGIN_TOKENS = 1024


@dataclass(frozen=True)
class TokenPreflightResult:
    """一次最终 ModelContext 的容量检查结果。"""

    token_count: int
    source: TokenCountSource
    effective_input_capacity: int | None
    safety_margin_tokens: int
    threshold: int | None
    compaction_required: bool
    estimated_upper_bound_tokens: int | None = None


class ContextCompactionRequired(RuntimeError):
    """ModelContext 达到安全阈值；交由后续 HistoryCompaction 流程处理。"""

    def __init__(self, result: TokenPreflightResult) -> None:
        self.result = result
        capacity = result.effective_input_capacity
        threshold = result.threshold
        super().__init__(
            "ModelContext compaction required: "
            f"{result.token_count} tokens >= threshold {threshold} "
            f"(effective input capacity {capacity}, "
            f"source={result.source})"
        )


async def preflight_model_context(
    *,
    context: ModelContext,
    provider: Any,
    effective_input_capacity: int | None,
    safety_margin_tokens: int = DEFAULT_SAFETY_MARGIN_TOKENS,
) -> TokenPreflightResult:
    """检查最终 Context，超阈值时抛出唯一的 compaction-required 信号。

    Provider 能按自身 wire 语义计数时优先采用该结果。Provider 缺少计数能力、
    返回 ``None`` 或计数失败时，使用整个 Provider-neutral ModelContext JSON 的
    UTF-8 字节数作为保守上界（每个 token 至少包含一个字节），并明确标记来源。
    该函数永远不截断 Context，也不会重试或循环构建 Context。
    """
    _validate_inputs(effective_input_capacity, safety_margin_tokens)
    estimated_upper_bound = _conservative_estimate(context)
    token_count = await _provider_count(provider, context)
    if token_count is None:
        token_count = estimated_upper_bound
        source: TokenCountSource = "conservative_estimate"
    else:
        source = "provider"

    threshold = (
        max(0, effective_input_capacity - safety_margin_tokens)
        if effective_input_capacity is not None
        else None
    )
    compaction_required = threshold is not None and token_count >= threshold
    result = TokenPreflightResult(
        token_count=token_count,
        source=source,
        effective_input_capacity=effective_input_capacity,
        safety_margin_tokens=safety_margin_tokens,
        threshold=threshold,
        compaction_required=compaction_required,
        estimated_upper_bound_tokens=(
            estimated_upper_bound if source == "conservative_estimate" else None
        ),
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
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _conservative_estimate(context: ModelContext) -> int:
    serialized = json.dumps(
        context.to_dict(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return max(1, len(serialized.encode("utf-8")))


def _validate_inputs(
    effective_input_capacity: int | None, safety_margin_tokens: int
) -> None:
    if effective_input_capacity is not None and (
        isinstance(effective_input_capacity, bool) or effective_input_capacity < 0
    ):
        raise ValueError("effective_input_capacity 必须为非负整数或 None")
    if isinstance(safety_margin_tokens, bool) or safety_margin_tokens < 0:
        raise ValueError("safety_margin_tokens 不能小于 0")
