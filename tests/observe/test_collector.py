"""采集器：turn 切分、tool 配对、usage 合计的正确性矩阵。"""

from types import SimpleNamespace

from pickel.conversations.agent_message import (
    AssistantMessage,
    ModelResponseMetadata,
    ModelUsage,
    ToolResultMessage,
    UserMessage,
)
from pickel.conversations.content_blocks import (
    TextContent,
    ThinkingContent,
    ToolCallContent,
)
from pickel.conversations.session import Session
from pickel.observe.collector import collect_trajectory


def _assistant(
    *,
    blocks=None,
    text: str | None = "ok",
    input_tokens: int = 100,
    output_tokens: int = 10,
    cache_read: int | None = None,
    cache_write: int | None = None,
    elapsed_ms: int = 500,
    usage: bool = True,
) -> AssistantMessage:
    content = list(blocks or [])
    if text is not None:
        content.append(TextContent(text=text))
    return AssistantMessage(
        content=content,
        metadata=ModelResponseMetadata(
            provider="anthropic",
            model="claude-sonnet-5",
            finish_reason="stop",
            elapsed_ms=elapsed_ms,
            usage=(
                ModelUsage(
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cache_read_tokens=cache_read,
                    cache_write_tokens=cache_write,
                )
                if usage
                else None
            ),
        ),
    )


def _user(text: str) -> UserMessage:
    return UserMessage(content=[TextContent(text=text)])


def test_single_turn_two_steps_with_tool_pairing():
    session = Session.create(agent_id="Pickle")
    session.append_user(_user("列文件"))
    session.append_assistant(
        _assistant(
            blocks=[
                ThinkingContent(text="想一想"),
                ToolCallContent(id="c1", name="shell_exec", arguments={"command": "ls"}),
            ],
            text=None,
            input_tokens=1000,
            cache_read=200,
            cache_write=30,
            output_tokens=40,
        )
    )
    session.append_tool_result(
        ToolResultMessage(
            tool_call_id="c1",
            tool_name="shell_exec",
            content=[TextContent(text="a.txt\nb.txt")],
        )
    )
    session.append_assistant(_assistant(text="完成", input_tokens=1200, output_tokens=20))

    trajectory = collect_trajectory(session)

    assert len(trajectory.turns) == 1
    turn = trajectory.turns[0]
    assert turn.query == "列文件"
    assert len(turn.steps) == 2
    assert turn.steps[0].thinking_chars == 3
    execution = turn.steps[0].tool_executions[0]
    assert execution.result_preview == "a.txt\nb.txt"
    assert execution.is_error is False
    assert execution.orphan is False
    assert turn.final_text == "完成"
    assert turn.usage_totals == {
        "input": 2200,
        "cache_read": 200,
        "cache_write": 30,
        "output": 60,
        "actual_input": 2430,
    }
    assert turn.elapsed_ms == 1000
    assert trajectory.session_usage["actual_input"] == 2430


def test_multi_turn_split():
    session = Session.create(agent_id="Pickle")
    for index in range(3):
        session.append_user(_user(f"q{index}"))
        session.append_assistant(_assistant(text=f"a{index}"))

    trajectory = collect_trajectory(session)

    assert [turn.index for turn in trajectory.turns] == [0, 1, 2]
    assert [turn.query for turn in trajectory.turns] == ["q0", "q1", "q2"]


def test_orphan_tool_result_kept():
    session = Session.create(agent_id="Pickle")
    session.append_user(_user("hi"))
    session.append_assistant(_assistant(text="ok"))
    session.append_tool_result(
        ToolResultMessage(
            tool_call_id="ghost",
            tool_name="shell_exec",
            content=[TextContent(text="孤儿结果")],
            is_error=True,
        )
    )

    trajectory = collect_trajectory(session)

    executions = trajectory.turns[0].steps[0].tool_executions
    assert len(executions) == 1
    assert executions[0].orphan is True
    assert executions[0].is_error is True


def test_assistant_without_usage_counts_zero():
    session = Session.create(agent_id="Pickle")
    session.append_user(_user("hi"))
    session.append_assistant(_assistant(usage=False))

    trajectory = collect_trajectory(session)

    assert trajectory.turns[0].usage_totals["actual_input"] == 0
    assert trajectory.turns[0].steps[0].usage["input"] == 0


def test_compaction_records_step_index():
    session = Session.create(agent_id="Pickle")
    session.append_user(_user("q0"))
    session.append_assistant(_assistant())
    session.append_compaction({"summary": "压缩了"})
    session.append_user(_user("q1"))
    session.append_assistant(_assistant())

    trajectory = collect_trajectory(session)

    assert trajectory.compaction_steps == [1]
    assert len(trajectory.turns) == 2


def test_result_preview_truncated():
    session = Session.create(agent_id="Pickle")
    session.append_user(_user("hi"))
    session.append_assistant(
        _assistant(
            blocks=[ToolCallContent(id="c1", name="shell_exec", arguments={})],
            text=None,
        )
    )
    session.append_tool_result(
        ToolResultMessage(
            tool_call_id="c1",
            tool_name="shell_exec",
            content=[TextContent(text="x" * 3000)],
        )
    )

    trajectory = collect_trajectory(session)

    preview = trajectory.turns[0].steps[0].tool_executions[0].result_preview
    assert len(preview) == 2000


def test_leading_assistant_gets_anonymous_turn():
    session = Session.create(agent_id="Pickle")
    session.append_assistant(_assistant(text="自发消息"))

    trajectory = collect_trajectory(session)

    assert trajectory.turns[0].query == ""
    assert trajectory.turns[0].steps[0].text == "自发消息"


def test_trace_enhancement_applied():
    session = Session.create(agent_id="Pickle")
    session.append_user(_user("hi"))
    session.append_assistant(
        _assistant(
            blocks=[ToolCallContent(id="c1", name="shell_exec", arguments={})],
            text=None,
        )
    )

    enhancement = SimpleNamespace(
        tool_timings={
            "c1": SimpleNamespace(
                started_at="2026-07-26T00:00:00+00:00",
                completed_at="2026-07-26T00:00:01+00:00",
                duration_ms=1000,
            )
        },
        turn_markers=[
            SimpleNamespace(
                started_at="2026-07-26T00:00:00+00:00",
                failed={"error_type": "RuntimeError", "message": "boom"},
                interrupted=False,
            )
        ],
    )

    trajectory = collect_trajectory(session, enhancement=enhancement)

    execution = trajectory.turns[0].steps[0].tool_executions[0]
    assert execution.duration_ms == 1000
    assert trajectory.turns[0].failed == {"error_type": "RuntimeError", "message": "boom"}
    assert trajectory.trace_available is True


def test_turn_marker_count_mismatch_skips_turn_enhancement():
    session = Session.create(agent_id="Pickle")
    session.append_user(_user("hi"))
    session.append_assistant(_assistant())

    enhancement = SimpleNamespace(tool_timings={}, turn_markers=[])

    trajectory = collect_trajectory(session, enhancement=enhancement)

    assert trajectory.turns[0].started_at is None
    assert trajectory.turns[0].failed is None


def test_out_of_order_results_pair_by_id():
    """两个 call 的结果乱序到达:必须按 id 配对,按序配对会错位。"""
    session = Session.create(agent_id="Pickle")
    session.append_user(_user("hi"))
    session.append_assistant(
        _assistant(
            blocks=[
                ToolCallContent(id="c1", name="tool_a", arguments={}),
                ToolCallContent(id="c2", name="tool_b", arguments={}),
            ],
            text=None,
        )
    )
    session.append_tool_result(
        ToolResultMessage(
            tool_call_id="c2",
            tool_name="tool_b",
            content=[TextContent(text="结果B")],
        )
    )
    session.append_tool_result(
        ToolResultMessage(
            tool_call_id="c1",
            tool_name="tool_a",
            content=[TextContent(text="结果A")],
        )
    )

    trajectory = collect_trajectory(session)

    executions = trajectory.turns[0].steps[0].tool_executions
    by_id = {execution.tool_call_id: execution for execution in executions}
    assert by_id["c1"].result_preview == "结果A"
    assert by_id["c2"].result_preview == "结果B"
    assert not any(execution.orphan for execution in executions)


def test_request_digest_backfilled_per_step():
    session = Session.create(agent_id="Pickle")
    session.append_user(_user("hi"))
    session.append_assistant(_assistant())
    session.append_assistant(_assistant())

    digest0 = {
        "system_sections": [{"name": "behavior", "chars": 100}],
        "tool_names": ["shell_exec"],
        "message_count": 1,
        "request_chars": 400,
        "hook_injected_chars": 0,
    }
    digest1 = {**digest0, "message_count": 2, "request_chars": 900}
    enhancement = SimpleNamespace(
        tool_timings={},
        turn_markers=[
            SimpleNamespace(started_at=None, failed=None, interrupted=False)
        ],
        request_digests=[[digest0, digest1]],
    )

    trajectory = collect_trajectory(session, enhancement=enhancement)

    steps = trajectory.turns[0].steps
    assert steps[0].request_digest == digest0
    assert steps[1].request_digest == digest1


def test_request_digest_count_mismatch_skipped():
    session = Session.create(agent_id="Pickle")
    session.append_user(_user("hi"))
    session.append_assistant(_assistant())
    session.append_assistant(_assistant())

    enhancement = SimpleNamespace(
        tool_timings={},
        turn_markers=[
            SimpleNamespace(started_at=None, failed=None, interrupted=False)
        ],
        request_digests=[[{"message_count": 1}]],
    )

    trajectory = collect_trajectory(session, enhancement=enhancement)

    assert all(
        step.request_digest is None for step in trajectory.turns[0].steps
    )


def test_enhancement_without_request_digests_attr_tolerated():
    """旧 TraceEnhancement(无 request_digests 属性)不应崩。"""
    session = Session.create(agent_id="Pickle")
    session.append_user(_user("hi"))
    session.append_assistant(_assistant())

    enhancement = SimpleNamespace(tool_timings={}, turn_markers=[])

    trajectory = collect_trajectory(session, enhancement=enhancement)

    assert trajectory.turns[0].steps[0].request_digest is None
