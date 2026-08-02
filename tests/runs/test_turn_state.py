from pickel.context.hook_feedback import HookFeedback
from pickel.runs.turn_state import TurnState


def test_step_hook_feedback_is_consumed_by_next_step() -> None:
    state = TurnState()
    feedback = HookFeedback(source_event="UserPromptSubmit", text="补充约束")
    state.step_hook_feedback.append(feedback)

    state.begin_step(1)

    assert state.hook_feedback_for_current_step() == [feedback]
    assert state.hook_feedback_for_current_step() == []
