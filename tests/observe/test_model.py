"""轨迹值对象：可 JSON 化是 HTML 内嵌数据的前提。"""

import json

from pickel.observe.model import (
    SessionTrajectory,
    Step,
    ToolExecution,
    Turn,
    trajectory_to_dict,
)


def test_trajectory_json_roundtrip():
    step = Step(
        index=0,
        thinking_chars=12,
        text="好的",
        tool_executions=[
            ToolExecution(
                tool_call_id="c1",
                name="shell_exec",
                arguments={"command": "ls"},
                result_preview="ok",
                is_error=False,
            )
        ],
        model_label="anthropic / claude-sonnet-5",
        finish_reason="tool_calls",
        usage={
            "input": 100,
            "cache_read": 50,
            "cache_write": 10,
            "output": 20,
            "actual_input": 160,
        },
        elapsed_ms=1500,
        hook_injected_chars=0,
        context_fingerprint="abc",
    )
    turn = Turn(
        index=0,
        query="列一下文件",
        steps=[step],
        final_text="好的",
        usage_totals={
            "input": 100,
            "cache_read": 50,
            "cache_write": 10,
            "output": 20,
            "actual_input": 160,
        },
        elapsed_ms=1500,
    )
    trajectory = SessionTrajectory(
        session_id="s1",
        agent_id="Pickle",
        cwd="/tmp",
        title=None,
        created_at="2026-07-26T00:00:00+00:00",
        updated_at="2026-07-26T00:00:01+00:00",
        turns=[turn],
        compaction_steps=[],
        session_usage={
            "input": 100,
            "cache_read": 50,
            "cache_write": 10,
            "output": 20,
            "actual_input": 160,
        },
        trace_available=False,
    )

    data = json.loads(json.dumps(trajectory_to_dict(trajectory)))
    assert data["session_id"] == "s1"
    assert data["turns"][0]["steps"][0]["usage"]["actual_input"] == 160
    assert data["turns"][0]["steps"][0]["tool_executions"][0]["orphan"] is False
