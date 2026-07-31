"""HTML 报告:自包含、数据岛可还原、防脚本注入。"""

import json

import pytest

from pickel.observe.html_report import render_html
from pickel.observe.model import SessionTrajectory, Step, Turn


def _trajectory(*, session_id: str = "s1", text: str = "好的") -> SessionTrajectory:
    usage = {
        "input": 100,
        "cache_read": 0,
        "cache_write": 0,
        "output": 10,
        "actual_input": 100,
    }
    step = Step(
        index=0,
        thinking_chars=0,
        text=text,
        tool_executions=[],
        model_label="anthropic / claude-sonnet-5",
        finish_reason="stop",
        usage=usage,
        elapsed_ms=800,
        hook_injected_chars=0,
        context_fingerprint=None,
    )
    turn = Turn(
        index=0,
        query="你好",
        steps=[step],
        final_text=text,
        usage_totals=usage,
        elapsed_ms=800,
    )
    return SessionTrajectory(
        session_id=session_id,
        agent_id="Pickle",
        cwd="/tmp",
        title="测试会话",
        created_at="2026-07-26T00:00:00+00:00",
        updated_at="2026-07-26T00:00:01+00:00",
        turns=[turn],
        compaction_steps=[],
        session_usage=usage,
        trace_available=False,
    )


def _extract_data(html: str) -> list:
    marker = 'id="observe-data">'
    start = html.index(marker) + len(marker)
    end = html.index("</script>", start)
    return json.loads(html[start:end].replace("<\\/", "</"))


def test_render_embeds_recoverable_json():
    html = render_html([_trajectory()], generated_at="2026-07-26T01:00:00+00:00")

    data = _extract_data(html)
    assert data[0]["session_id"] == "s1"
    assert data[0]["turns"][0]["query"] == "你好"


def test_no_external_resources():
    html = render_html([_trajectory()], generated_at="2026-07-26T01:00:00+00:00")

    assert 'src="http' not in html
    assert "src='http" not in html
    assert 'href="http' not in html


def test_script_close_tag_in_content_is_escaped():
    html = render_html(
        [_trajectory(text="坏内容</script><script>alert(1)</script>")],
        generated_at="2026-07-26T01:00:00+00:00",
    )

    data = _extract_data(html)
    assert "</script>" in data[0]["turns"][0]["final_text"]


def test_empty_list_raises():
    with pytest.raises(ValueError):
        render_html([], generated_at="2026-07-26T01:00:00+00:00")


def test_step_request_digest_rendered_in_template():
    """模板 JS 须消费 request_digest 并有对应 UI(请求摘要)。"""
    trajectory = _trajectory()
    html = render_html([trajectory], generated_at="2026-07-27T00:00:00+00:00")

    assert "s.request_digest" in html
    assert "请求摘要" in html
    data = _extract_data(html)
    assert "request_digest" in data[0]["turns"][0]["steps"][0]


def test_full_request_snapshot_has_anthropic_cache_order_ui():
    html = render_html([_trajectory()], generated_at="2026-07-31T00:00:00+00:00")

    assert "s.request_snapshot" in html
    assert "完整 Provider 请求" in html
    assert '["tools", "system", "messages"]' in html
    assert "自动 breakpoint" in html
