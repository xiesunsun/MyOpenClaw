"""ContextRenderer：新版 Context 面板 + total 来源标注。"""

from __future__ import annotations

from rich.console import Console

from pickel.cli.context_renderer import ContextRenderer
from pickel.runs.measure import ContextCategory, ContextDetail, ContextUsage


def _usage(
    source: str = "estimated",
    *,
    total: int = 2490,
    max_input: int = 200_000,
) -> ContextUsage:
    free = max_input - total
    return ContextUsage(
        model_label="anthropic / claude-jupiter-v1-p",
        total_tokens=total,
        total_source=source,  # type: ignore[arg-type]
        max_input_tokens=max_input,
        categories=[
            ContextCategory(key="behavior", label="System prompt", tokens=100),
            ContextCategory(key="skills_guidance", label="Skills guidance", tokens=50),
            ContextCategory(
                key="skills_catalog",
                label="Skills catalog",
                tokens=200,
                details=[
                    ContextDetail(label="- skill-a", tokens=100),
                    ContextDetail(label="- skill-b", tokens=100),
                ],
            ),
            ContextCategory(key="messages", label="Messages", tokens=1000),
            ContextCategory(key="tools", label="Tools", tokens=1000),
            ContextCategory(key="other", label="Other", tokens=140),
        ],
        free_tokens=free,
    )


def _render(
    source: str = "estimated",
    *,
    total: int = 2490,
    max_input: int = 200_000,
    **kwargs,
) -> str:
    console = Console(width=100, record=True, force_terminal=False)
    console.print(
        ContextRenderer().render(
            _usage(source, total=total, max_input=max_input),
            tool_definitions=12,
            turns=1,
            tool_calls=2,
            compactions=0,
            **kwargs,
        )
    )
    return console.export_text()


def test_标题为_Context_与紧凑总量():
    text = _render()
    assert "Context" in text
    assert "2.5k / 200k tokens" in text
    assert "anthropic / claude-jupiter-v1-p" in text


def test_菱形占用图存在():
    text = _render(total=100_000, max_input=200_000)
    assert "◆" in text
    assert "◇" in text


def test_主分栏与次分栏标记():
    text = _render()
    assert "◆ System prompt" in text
    assert "◆ Messages" in text
    assert "◇ Free" in text
    assert "◈ Tool definitions" in text
    assert "12 tools" in text
    assert "◈ Skills" in text
    assert "2 skills" in text
    # 不再默认展开 per-skill 长列表
    assert "skill-a" not in text


def test_会话统计行():
    text = _render()
    assert "Turns: 1 · Tool calls: 2 · Compactions: 0" in text


def test_无_auto_compact_假消息_但有_remaining():
    """未实现自动压缩；remaining 用 free_tokens 真数据。"""
    text = _render(total=1000, max_input=100_000)
    assert "Auto-compact" not in text
    assert "Remaining" in text
    assert "tokens free" in text
    # free = 99000 → 99k
    assert "99k" in text


def test_菱形实心为占用_空心为剩余():
    """半满：约一半 ◆、一半 ◇。"""
    text = _render(total=50_000, max_input=100_000)
    filled = text.count("◆")
    empty = text.count("◇")
    # 分栏里还有 ◆ Messages 等标记，图上 100 格约 50 实心
    assert filled >= 45
    assert empty >= 45


def test_每档来源各有不同标注():
    labels = {
        source: _render(source)
        for source in ("anchor", "anchor_plus_tail", "counted", "estimated")
    }
    markers = []
    for source, text in labels.items():
        if source == "anchor":
            assert "真实 usage 锚" in text
            assert "尾部" not in text.split("真实 usage 锚")[0]
        markers.append(
            next(
                line
                for line in text.splitlines()
                if "measured" in line or "counted" in line or "estimated" in line
            )
        )
    assert len(set(markers)) == 4


def test_anchor_标注为真实_usage_锚():
    assert "真实 usage 锚" in _render("anchor")


def test_anchor_plus_tail_说明含尾部估计():
    text = _render("anchor_plus_tail")
    assert "锚" in text
    assert "尾部估计" in text


def test_counted_不得声称来自锚():
    text = _render("counted")
    assert "锚" not in text
    assert "counted" in text


def test_estimated_标注为本地估计():
    text = _render("estimated")
    assert "estimated" in text
    assert "本地估计" in text
    assert "锚" not in text
