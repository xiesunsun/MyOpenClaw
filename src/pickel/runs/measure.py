"""measure：Request(+锚) → ContextUsage（设计 §6）。

合同：
- total 靠锚拿准（§6.1 三档），分栏靠本地估计拿快（§6.2）
- 除 C 档外不发起网络请求；分栏永不远程
- 无状态、不写 Session
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from pickel.context.model_context import ModelContext
from pickel.runs.estimator import estimate_messages, estimate_text, estimate_tools
from pickel.runs.usage_anchor import UsageAnchor

TotalSource = Literal["anchor", "anchor_plus_tail", "counted", "estimated"]

_SECTION_LABELS = {
    "behavior": "System prompt",
    "skills_guidance": "Skills guidance",
    "skills_catalog": "Skills catalog",
}


@dataclass(frozen=True)
class ContextDetail:
    label: str
    tokens: int


@dataclass(frozen=True)
class ContextCategory:
    key: str
    label: str
    tokens: int
    details: list[ContextDetail] = field(default_factory=list)


@dataclass(frozen=True)
class ContextUsage:
    """一次上下文占用快照（值对象，不落盘）。"""

    model_label: str
    total_tokens: int
    total_source: TotalSource
    max_input_tokens: int | None
    categories: list[ContextCategory]
    free_tokens: int | None

    @property
    def is_measured(self) -> bool:
        """total 是否来自真实 usage 或远程计数（分栏永远是估计）。"""
        return self.total_source in ("anchor", "counted")

    def category(self, key: str) -> ContextCategory:
        for category in self.categories:
            if category.key == key:
                return category
        raise KeyError(key)


async def measure(
    *,
    request: ModelContext,
    anchor: UsageAnchor | None,
    provider: Any,
    model_config: Any,
) -> ContextUsage:
    """组装 ContextUsage。"""
    raw = _raw_categories(request)
    raw_total = sum(tokens for _, tokens, _ in raw)

    total, source = await _resolve_total(
        request=request,
        anchor=anchor,
        provider=provider,
        raw_total=raw_total,
    )
    categories = _normalize(raw, total=total)

    max_input_tokens = getattr(model_config, "max_input_tokens", None)
    return ContextUsage(
        model_label=f"{model_config.provider} / {model_config.model}",
        total_tokens=total,
        total_source=source,
        max_input_tokens=max_input_tokens,
        categories=categories,
        free_tokens=(
            max_input_tokens - total if max_input_tokens is not None else None
        ),
    )


async def _resolve_total(
    *,
    request: ModelContext,
    anchor: UsageAnchor | None,
    provider: Any,
    raw_total: int,
) -> tuple[int, TotalSource]:
    """§6.1 三档。"""
    if anchor is not None:
        if not anchor.trailing_messages:
            return anchor.next_request_base, "anchor"
        tail = estimate_messages(anchor.trailing_messages)
        return anchor.next_request_base + tail, "anchor_plus_tail"

    # C 档：空 messages 不得远程 count（Anthropic count_tokens 会拒空 messages）
    if not request.messages:
        return raw_total, "estimated"

    counted = await provider.count_context_tokens(request)
    if counted is None:
        return raw_total, "estimated"
    return int(counted), "counted"


def _raw_categories(
    request: ModelContext,
) -> list[tuple[str, int, list[ContextDetail]]]:
    """(key, 原始估计, 明细) 列表；顺序即 UI 展示顺序。"""
    rows: list[tuple[str, int, list[ContextDetail]]] = []
    for section in request.system.sections:
        rows.append(
            (
                section.name,
                estimate_text(section.text),
                _section_details(section),
            )
        )
    rows.append(("messages", estimate_messages(request.messages), []))
    rows.append(("tools", estimate_tools(request.tools), []))
    return rows


def _section_details(section: Any) -> list[ContextDetail]:
    """skills catalog 按行给 per-skill 明细；一律本地估计，不做远程差分。"""
    if section.name != "skills_catalog":
        return []
    return [
        ContextDetail(label=line.strip(), tokens=estimate_text(line))
        for line in section.text.splitlines()
        if line.strip().startswith("-")
    ]


def _normalize(
    raw: list[tuple[str, int, list[ContextDetail]]],
    *,
    total: int,
) -> list[ContextCategory]:
    """把原始估计按 total 归一化；栏位非负，且与 other 之和恒等于 total。"""
    raw_total = sum(tokens for _, tokens, _ in raw)
    scale = (total / raw_total) if raw_total > 0 else 0.0

    categories: list[ContextCategory] = []
    assigned = 0
    for key, tokens, details in raw:
        scaled = max(0, round(tokens * scale))
        # 逐项累加时夹住上限，保证 other 不为负
        scaled = min(scaled, max(0, total - assigned))
        assigned += scaled
        categories.append(
            ContextCategory(
                key=key,
                label=_SECTION_LABELS.get(key, key.replace("_", " ").capitalize()),
                tokens=scaled,
                details=_scale_details(details, scale=scale),
            )
        )

    categories.append(
        ContextCategory(
            key="other",
            label="Other",
            tokens=max(0, total - assigned),
        )
    )
    return categories


def _scale_details(
    details: list[ContextDetail],
    *,
    scale: float,
) -> list[ContextDetail]:
    if not details:
        return []
    return [
        ContextDetail(label=detail.label, tokens=max(0, round(detail.tokens * scale)))
        for detail in details
    ]
