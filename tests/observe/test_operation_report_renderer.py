from __future__ import annotations

import json
import re

from pickel.observe.operation_report_renderer import OperationReportRenderer


def test_operation_report_renderer_renders_html_structure() -> None:
    renderer = OperationReportRenderer()

    doc_data = {
        "session": {
            "session_id": "sess_test",
            "agent_id": "Pickle",
            "workspace_id": "ws_test",
            "cwd": "/workspace",
        },
        "operation": {
            "operation_id": "op_test_123",
            "status": "succeeded",
        },
        "summary": {
            "status": "succeeded",
            "duration_text": "12.4s",
            "model_calls_count": 2,
            "model_retries_count": 1,
            "tool_calls_count": 1,
            "children_count": 0,
            "trace_integrity": "可靠事实完整 · Trace 轨迹完整",
        },
        "model_calls": [
            {
                "key": "call1",
                "model_call_id": "mc_1",
                "attempt": 1,
                "status": "completed",
                "timing": {"latency_ms": 3200, "ttft_ms": 400},
                "usage": {
                    "input_tokens": 500,
                    "cache_read_tokens": 100,
                    "cache_hit_rate": 20.0,
                    "total_tokens": 600,
                },
                "finish_reason": "stop",
                "request_content": {"is_ok": True},
                "response_content": {"is_ok": True},
            }
        ],
        "execution_nodes": [
            {
                "key": "operation",
                "kind": "operation",
                "label": "Operation 01",
                "meta": "succeeded",
                "status": "ok",
                "depth": 0,
            },
            {
                "key": "call1",
                "kind": "model",
                "label": "ModelCall 01",
                "meta": "3.2s",
                "status": "ok",
                "depth": 1,
            },
        ],
        "timeline": {
            "start_time_iso": "2026-08-27T10:00:00Z",
            "end_time_iso": "2026-08-27T10:00:12Z",
            "total_duration_ms": 12400,
            "axis_ticks": ["0s", "3s", "6s", "9s", "12s"],
            "lanes": [
                {
                    "name": "Model",
                    "bars": [
                        {
                            "key": "call1",
                            "kind": "model",
                            "label": "Call 01",
                            "duration_text": "3.2s",
                            "status": "ok",
                            "left_pct": 10,
                            "width_pct": 30,
                        }
                    ],
                }
            ],
            "critical_path_text": "正常执行路径",
        },
        "charts": {
            "latency": [
                {"key": "call1", "label": "Call 1", "value": 3.2, "status": "completed"}
            ],
            "cache": [
                {
                    "key": "call1",
                    "label": "Call 1",
                    "value": 20.0,
                    "status": "completed",
                    "formula": "cache_read / (input + cache_read)",
                    "denominator": "input + cache_read",
                    "source": "anthropic.usage.cache_read_input_tokens",
                }
            ],
            "tokens": [
                {
                    "key": "call1",
                    "label": "Call 1",
                    "total": 600,
                    "input": 500,
                    "cached": 100,
                    "uncached": 400,
                    "output": 100,
                    "status": "completed",
                }
            ],
        },
        "document_evidence": {
            "call1": {
                "label": "ModelCall 01",
                "model_call_id": "mc_1",
                "request_content_ref": "ref_req",
                "response_content_ref": "ref_resp",
                "context": {
                    "label": "RequestContent.model_context",
                    "sections": [
                        {
                            "id": "system",
                            "label": "system",
                            "count": "1",
                            "path": "model_context.system",
                            "complete": "完整",
                            "value": {"blocks": []},
                        }
                    ],
                },
                "wire": {"label": "RequestContent.wire_request", "sections": []},
                "provider": {
                    "label": "ResponseContent.provider_response",
                    "sections": [],
                },
                "assistant": {
                    "label": "ResponseContent.assistant_message",
                    "sections": [],
                },
            }
        },
        "trace_integrity": "可靠事实完整 · Trace 轨迹完整",
    }

    html_content = renderer.render(doc_data)

    assert "<!doctype html>" in html_content
    assert "Pickel Diagnostics · Operation Trace Explorer" in html_content
    assert '<div id="pickel-trace-explorer">' in html_content
    assert "pk-analytics" in html_content
    assert "pk-timeline" in html_content
    assert "pk-evidence" in html_content
    assert '<script id="pk-observation-data" type="application/json">' in html_content
    assert "op_test_123" in html_content
    assert "mc_1" in html_content
    assert "pk-selected-detail" in html_content
    assert "pk-inspector" not in html_content
    assert "cache read ÷ input" not in html_content
    assert "provider-defined / unknown" in html_content
    assert "formula" in html_content
    assert "denominator" in html_content
    assert "source" in html_content
    assert "connect-src 'self'" in html_content
    assert "function renderSummary()" in html_content


def test_renderer_embedded_observation_is_parseable_and_script_safe() -> None:
    payload = {
        "operation": {"operation_id": "op-</script>&<tag>"},
        "session": {"cwd": "line\u2028separator\u2029</script>"},
    }

    rendered = OperationReportRenderer().render(payload)
    match = re.search(
        r'<script id="pk-observation-data" type="application/json">\s*(.*?)\s*</script>',
        rendered,
        flags=re.DOTALL,
    )

    assert match is not None
    embedded = match.group(1)
    decoded = json.loads(embedded)
    assert decoded == payload
    assert "</script>" not in embedded
    assert "<tag>" not in embedded
    assert "&<" not in embedded
    assert "\u2028" not in embedded
    assert "\u2029" not in embedded
