import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from pickel.app.runtime_host import _RuntimeDelegationControl
from pickel.operations.delegation_service import DelegationService
from pickel.operations.operation_service import OperationService
from pickel.persistence.errors import StorageConflictError


def test_runtime_control_cancels_and_wakes_active_child(monkeypatch) -> None:
    store = SimpleNamespace(
        load_session=lambda session_id: SimpleNamespace(
            active_operation_id="child-operation"
        ),
        load_run_state=lambda operation_id: SimpleNamespace(status="cancelling"),
    )
    host = SimpleNamespace(
        activate_agent=AsyncMock(),
        agent_registry=SimpleNamespace(wake=Mock()),
    )
    monkeypatch.setattr(
        DelegationService,
        "prepare_cancel_delegation",
        lambda self, *args: "child-operation",
    )
    monkeypatch.setattr(
        OperationService,
        "request_cancellation",
        lambda self, operation_id, *, reason: True,
    )

    result = asyncio.run(
        _RuntimeDelegationControl(host, store).cancel_delegation(
            sender_operation_id="parent-operation",
            sender_step_id="step-1",
            sender_tool_call_id="tool-1",
            target_child_session_id="child-session",
        )
    )

    assert result == "child-operation"
    assert host.activate_agent.await_count >= 1
    host.agent_registry.wake.assert_called_once_with("child-session")


def test_runtime_control_does_not_activate_idle_child(monkeypatch) -> None:
    store = SimpleNamespace(
        load_session=lambda session_id: SimpleNamespace(active_operation_id=None),
        load_run_state=lambda operation_id: None,
    )
    host = SimpleNamespace(
        activate_agent=AsyncMock(), agent_registry=SimpleNamespace(wake=Mock())
    )
    monkeypatch.setattr(
        DelegationService,
        "prepare_cancel_delegation",
        lambda self, *args: None,
    )

    result = asyncio.run(
        _RuntimeDelegationControl(host, store).cancel_delegation(
            sender_operation_id="parent-operation",
            sender_step_id="step-1",
            sender_tool_call_id="tool-1",
            target_child_session_id="child-session",
        )
    )

    assert result is None
    host.activate_agent.assert_not_awaited()
    host.agent_registry.wake.assert_not_called()


def test_runtime_control_retries_cancellation_cas_and_rejects_second_failure(
    monkeypatch,
) -> None:
    store = SimpleNamespace(
        load_session=lambda session_id: SimpleNamespace(
            active_operation_id="child-operation"
        ),
        load_run_state=lambda operation_id: SimpleNamespace(status="running"),
    )
    host = SimpleNamespace(
        activate_agent=AsyncMock(), agent_registry=SimpleNamespace(wake=Mock())
    )
    monkeypatch.setattr(
        DelegationService,
        "prepare_cancel_delegation",
        lambda self, *args: "child-operation",
    )
    request = Mock(side_effect=[False, False])
    monkeypatch.setattr(OperationService, "request_cancellation", request)

    try:
        asyncio.run(
            _RuntimeDelegationControl(host, store).cancel_delegation(
                sender_operation_id="parent-operation",
                sender_step_id="step-1",
                sender_tool_call_id="tool-1",
                target_child_session_id="child-session",
            )
        )
    except StorageConflictError:
        pass
    else:
        raise AssertionError("第二次取消 CAS 失败必须向 Tool 暴露冲突")
    assert request.call_count == 2
    host.activate_agent.assert_not_awaited()


def test_runtime_control_does_not_cancel_a_later_operation_after_terminal_race(
    monkeypatch,
) -> None:
    store = SimpleNamespace(
        load_session=lambda session_id: SimpleNamespace(
            active_operation_id="child-operation"
        ),
        load_run_state=Mock(
            side_effect=[
                SimpleNamespace(status="succeeded"),
            ]
        ),
    )
    host = SimpleNamespace(
        activate_agent=AsyncMock(), agent_registry=SimpleNamespace(wake=Mock())
    )
    monkeypatch.setattr(
        DelegationService,
        "prepare_cancel_delegation",
        lambda self, *args: "child-operation",
    )
    request = Mock(return_value=False)
    monkeypatch.setattr(OperationService, "request_cancellation", request)

    result = asyncio.run(
        _RuntimeDelegationControl(host, store).cancel_delegation(
            sender_operation_id="parent-operation",
            sender_step_id="step-1",
            sender_tool_call_id="tool-1",
            target_child_session_id="child-session",
        )
    )

    assert result is None
    request.assert_called_once()
    host.activate_agent.assert_not_awaited()
