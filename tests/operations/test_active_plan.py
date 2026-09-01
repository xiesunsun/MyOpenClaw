import pytest

from pickel.operations.active_plan import (
    ActivePlan,
    PlanItem,
    parse_active_plan,
    render_active_plan,
)
from pickel.operations.agent_run_state import AgentRunState


def test_active_plan_round_trip_and_render() -> None:
    plan = ActivePlan(
        (
            PlanItem("分析现有实现", "completed"),
            PlanItem("实现状态持久化", "in_progress"),
            PlanItem("补充恢复测试", "pending"),
        )
    )
    assert parse_active_plan(plan.to_dict()) == plan
    assert render_active_plan(plan) == (
        "<active_plan>\n\n# Work Plan\n\n"
        "- [x] 分析现有实现\n- [~] 实现状态持久化\n- [ ] 补充恢复测试\n\n"
        "</active_plan>"
    )


@pytest.mark.parametrize(
    "items",
    [[], [{"step": "", "status": "pending"}], [{"step": "x", "status": "bad"}]],
)
def test_active_plan_rejects_invalid_items(items: list[dict[str, str]]) -> None:
    with pytest.raises((TypeError, ValueError)):
        parse_active_plan({"items": items})


def test_active_plan_rejects_duplicate_in_progress_and_too_many_items() -> None:
    with pytest.raises(ValueError):
        parse_active_plan(
            {
                "items": [
                    {"step": "a", "status": "in_progress"},
                    {"step": "b", "status": "in_progress"},
                ]
            }
        )
    with pytest.raises(ValueError):
        parse_active_plan(
            {"items": [{"step": str(i), "status": "pending"} for i in range(21)]}
        )


def test_all_completed_plan_is_cleared() -> None:
    assert (
        parse_active_plan({"items": [{"step": "done", "status": "completed"}]}) is None
    )


def test_agent_run_state_serializes_active_plan_and_rejects_terminal_plan() -> None:
    plan = ActivePlan((PlanItem("继续工作", "pending"),))
    state = AgentRunState(
        "operation-1", 1, "running", None, 0, None, None, None, None, plan
    )
    assert AgentRunState.from_json(state.to_json()) == state
    with pytest.raises(ValueError):
        AgentRunState("operation-1", 2, "failed", None, 0, None, None, None, None, plan)
