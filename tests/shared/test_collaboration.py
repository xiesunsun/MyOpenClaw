import pytest

from pickel.context.model_context import ModelContext, SystemContent
from pickel.context.model_context_builder import ModelContextBuilder
from pickel.shared.collaboration import CollaborationState


def test_plan_state_renders_read_only_instruction_and_normalizes_plan() -> None:
    state = CollaborationState(
        mode="plan",
        plan=("  inspect repository  ", "", "design changes"),
    )

    assert state.plan == ("inspect repository", "design changes")
    assert "Plan mode is active" in state.system_prompt()
    assert "inspect repository" in state.system_prompt()


def test_goal_state_requires_goal_and_renders_evidence_rule() -> None:
    with pytest.raises(ValueError, match="必须提供 goal"):
        CollaborationState(mode="goal")

    state = CollaborationState(mode="goal", goal="make tests pass")
    assert "make tests pass" in state.system_prompt()
    assert "可验证证据" in state.system_prompt()


def test_context_builder_adds_collaboration_as_separate_system_section() -> None:
    package = type(
        "Package",
        (),
        {"behavior_instruction": "base", "skills": (), "tools": ()},
    )()
    context = ModelContextBuilder().build_model_context(
        package=package,
        visible_messages=(),
        collaboration=CollaborationState(mode="plan"),
    )

    assert context.system.sections[-1].name == "collaboration_mode"
    assert "只能检查和读取" in context.system.sections[-1].text


def test_context_value_object_still_accepts_empty_tools() -> None:
    context = ModelContext(system=SystemContent(), messages=(), tools=())
    assert context.tools == ()
