from pickel.runtime.goal_verifier import parse_goal_verification


def test_goal_verifier_parses_strict_json() -> None:
    result = parse_goal_verification(
        '{"passed": true, "reason": "tests pass", "nextAction": "stop"}'
    )

    assert result.passed is True
    assert result.reason == "tests pass"
    assert result.next_action == "stop"


def test_goal_verifier_fails_closed_on_invalid_response() -> None:
    result = parse_goal_verification("not json")

    assert result.passed is False
    assert "非法 JSON" in result.reason
