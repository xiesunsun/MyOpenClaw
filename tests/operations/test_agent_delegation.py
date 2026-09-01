from __future__ import annotations

from datetime import datetime, timezone

from pickel.operations.agent_delegation import AgentDelegation


def test_agent_delegation_json_round_trip_keeps_child_package_binding() -> None:
    delegation = AgentDelegation(
        child_session_id="child",
        child_package_version_id="child-package",
        parent_operation_id="parent-operation",
        parent_step_id="step",
        parent_tool_call_id="tool",
        initial_message_id="message",
        created_at=datetime(2026, 8, 27, tzinfo=timezone.utc),
    )

    assert AgentDelegation.from_json(delegation.to_json()) == delegation


def test_agent_delegation_json_requires_child_package_binding() -> None:
    value = '{"child_session_id":"child"}'

    try:
        AgentDelegation.from_json(value)
    except ValueError as exc:
        assert "missing" in str(exc)
    else:  # pragma: no cover - assertion documents the strict codec contract
        raise AssertionError("缺少 child_package_version_id 必须拒绝")
