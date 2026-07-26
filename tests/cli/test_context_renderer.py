"""ContextRenderer：total 来源标注必须逐档区分（设计 §6.1 / §7.2）。

真实 provider 验证暴露：counted 档被误标为「真实 usage 锚」，
但那一档的数字来自 provider 的 count_tokens，与锚无关。
"""

from __future__ import annotations

from rich.console import Console

from pickel.cli.context_renderer import ContextRenderer
from pickel.runs.measure import ContextCategory, ContextUsage


def _usage(source: str) -> ContextUsage:
    return ContextUsage(
        model_label="anthropic / claude-sonnet-5",
        total_tokens=2490,
        total_source=source,  # type: ignore[arg-type]
        max_input_tokens=200_000,
        categories=[ContextCategory(key="messages", label="Messages", tokens=2490)],
        free_tokens=197_510,
    )


def _render(source: str) -> str:
    console = Console(width=100, record=True, force_terminal=False)
    console.print(ContextRenderer().render(_usage(source)))
    return console.export_text()


def test_每档来源各有不同标注():
    labels = {source: _render(source) for source in
              ("anchor", "anchor_plus_tail", "counted", "estimated")}

    lines = {
        source: next(
            line.strip()
            for line in text.splitlines()
            if any(k in line for k in ("measured", "counted", "estimated"))
        )
        for source, text in labels.items()
    }
    assert len(set(lines.values())) == 4, lines


def test_anchor_标注为真实_usage_锚():
    assert "measured（真实 usage 锚）" in _render("anchor")


def test_anchor_plus_tail_说明含尾部估计():
    text = _render("anchor_plus_tail")
    assert "锚" in text
    assert "尾部估计" in text


def test_counted_不得声称来自锚():
    text = _render("counted")
    assert "锚" not in text
    assert "counted（provider 计数）" in text


def test_estimated_标注为本地估计():
    text = _render("estimated")
    assert "estimated（本地估计）" in text
    assert "锚" not in text
