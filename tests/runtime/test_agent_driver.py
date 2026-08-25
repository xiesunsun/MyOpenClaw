"""AgentDriver 显式 Operation 恢复入口的窄合同。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from pickel.persistence.errors import StorageConflictError
from pickel.runtime.agent import Agent
from pickel.runtime.agent_driver import AgentDriver


class _ConversationStore:
    def __init__(self, session) -> None:
        self.session = session

    def load_session(self, _session_id: str):
        return self.session


class _OperationDriver:
    def __init__(self, result) -> None:
        self.result = result
        self.calls = []

    async def drive_operation(self, operation_id: str, *, consume_delta, host_calls):
        self.calls.append((operation_id, consume_delta, host_calls))
        return self.result


class _Inbox:
    session_id = "session-1"


class _SerializedOperationDriver:
    def __init__(self) -> None:
        self.active = 0
        self.max_active = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.second_started = asyncio.Event()
        self.calls = 0

    async def drive_operation(self, _operation_id, *, consume_delta, host_calls):
        del consume_delta, host_calls
        self.calls += 1
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        if self.calls == 1:
            self.started.set()
            await self.release.wait()
        else:
            self.second_started.set()
        self.active -= 1
        return SimpleNamespace(status="waiting")


def _session(*, active_operation_id="operation-1", archived_at=None):
    return SimpleNamespace(
        active_operation_id=active_operation_id,
        active_node_id="node-1",
        archived_at=archived_at,
    )


def _driver(
    session,
    operation_driver,
    *,
    cancel_operation=None,
    wake_callback=None,
):
    return AgentDriver(
        conversation_store=_ConversationStore(session),
        inbox_store=object(),
        operation_service=object(),
        operation_driver=operation_driver,
        package_resolver=lambda **_: ("package-1", None),
        cancel_operation=cancel_operation,
        wake_callback=wake_callback,
    )


def test_resume_operation_requires_exact_active_operation_and_forwards_arguments():
    result = SimpleNamespace(status="waiting")
    operation_driver = _OperationDriver(result)
    driver = _driver(_session(), operation_driver)
    consume_delta = object()
    host_calls = object()

    resumed = asyncio.run(
        driver.resume_operation(
            session_id="session-1",
            operation_id="operation-1",
            consume_delta=consume_delta,
            host_calls=host_calls,
        )
    )

    assert resumed is result
    assert operation_driver.calls == [
        ("operation-1", consume_delta, host_calls),
    ]


@pytest.mark.parametrize("active_operation_id", [None, "operation-2"])
def test_resume_operation_rejects_non_matching_active_operation(active_operation_id):
    operation_driver = _OperationDriver(SimpleNamespace(status="succeeded"))
    driver = _driver(
        _session(active_operation_id=active_operation_id), operation_driver
    )

    with pytest.raises(StorageConflictError, match="active_operation_id"):
        asyncio.run(
            driver.resume_operation(session_id="session-1", operation_id="operation-1")
        )

    assert operation_driver.calls == []


def test_resume_operation_rejects_archived_session_before_driving():
    operation_driver = _OperationDriver(SimpleNamespace(status="succeeded"))
    driver = _driver(
        _session(
            active_operation_id=None,
            archived_at=datetime(2026, 8, 25, tzinfo=timezone.utc),
        ),
        operation_driver,
    )

    with pytest.raises(StorageConflictError, match="归档"):
        asyncio.run(
            driver.resume_operation(session_id="session-1", operation_id="operation-1")
        )

    assert operation_driver.calls == []


def test_agent_resume_operation_is_a_thin_proxy():
    result = SimpleNamespace(status="waiting")
    operation_driver = _OperationDriver(result)
    driver = _driver(_session(), operation_driver)
    agent = Agent(session_id="session-1", inbox=_Inbox(), driver=driver)
    consume_delta = object()
    host_calls = object()

    resumed = asyncio.run(
        agent.resume_operation(
            "operation-1",
            consume_delta=consume_delta,
            host_calls=host_calls,
        )
    )

    assert resumed is result
    assert operation_driver.calls == [
        ("operation-1", consume_delta, host_calls),
    ]


def test_agent_serializes_foreground_and_wake_drive_entries():
    async def scenario() -> None:
        operation_driver = _SerializedOperationDriver()
        agent = Agent(
            session_id="session-1",
            inbox=_Inbox(),
            driver=_driver(_session(), operation_driver),
        )
        foreground = asyncio.create_task(agent.when_idle())
        await operation_driver.started.wait()
        background = asyncio.create_task(agent.when_idle())
        operation_driver.release.set()
        await operation_driver.second_started.wait()
        await asyncio.gather(foreground, background)
        assert operation_driver.max_active == 1

    asyncio.run(scenario())


def test_idle_accepts_followup_after_inject_without_accepting_inject():
    class InboxStore:
        def list_pending(self, *, session_id):
            assert session_id == "session-1"
            return (
                SimpleNamespace(message_id="inject", delivery="inject"),
                SimpleNamespace(message_id="followup", delivery="followup"),
            )

    class Operations:
        def __init__(self):
            self.message = None

        def accept_pending_message(self, **kwargs):
            self.message = kwargs["message"]
            return SimpleNamespace(
                operation=SimpleNamespace(operation_id="operation-1")
            )

    operations = Operations()
    driver = AgentDriver(
        conversation_store=_ConversationStore(_session(active_operation_id=None)),
        inbox_store=InboxStore(),
        operation_service=operations,
        operation_driver=_OperationDriver(SimpleNamespace(status="succeeded")),
        package_resolver=lambda **_: ("package-1", None),
    )

    asyncio.run(driver.drive_once(session_id="session-1"))

    assert operations.message.message_id == "followup"


def test_agent_message_delivery_wakes_only_followup_and_steer():
    class Inbox:
        session_id = "session-1"

        async def send(self, message, *, delivery):
            return delivery

    class Driver:
        def __init__(self):
            self.wakes = []

        def wake(self, session_id):
            self.wakes.append(session_id)

        def cancel(self, **kwargs):
            return True

    async def scenario():
        driver = Driver()
        agent = Agent(session_id="session-1", inbox=Inbox(), driver=driver)
        await agent.followup(SimpleNamespace())
        await agent.steer(SimpleNamespace())
        await agent.inject(SimpleNamespace())
        assert driver.wakes == ["session-1", "session-1"]

    asyncio.run(scenario())


def test_agent_cancel_false_does_not_wake():
    class Inbox:
        session_id = "session-1"

    class Driver:
        def __init__(self):
            self.wakes = 0

        def cancel(self, **kwargs):
            return False

        def wake(self, session_id):
            del session_id
            self.wakes += 1

    driver = Driver()
    agent = Agent(session_id="session-1", inbox=Inbox(), driver=driver)

    assert not agent.cancel(reason="not active")
    assert driver.wakes == 0


@pytest.mark.parametrize("active_operation_id", [None])
def test_driver_cancel_without_active_operation_is_idempotent_without_wake(
    active_operation_id,
):
    wakes = []
    cancel_calls = []
    driver = _driver(
        _session(active_operation_id=active_operation_id),
        _OperationDriver(SimpleNamespace(status="succeeded")),
        cancel_operation=lambda operation_id, *, reason: cancel_calls.append(
            (operation_id, reason)
        ),
        wake_callback=wakes.append,
    )

    assert driver.cancel(session_id="session-1", reason="idle") is True
    assert cancel_calls == []
    assert wakes == []


@pytest.mark.parametrize("cancel_result", [True, False])
def test_driver_cancel_wakes_only_after_active_cancel_succeeds(cancel_result):
    wakes = []
    driver = _driver(
        _session(active_operation_id="operation-1"),
        _OperationDriver(SimpleNamespace(status="succeeded")),
        cancel_operation=lambda operation_id, *, reason: cancel_result,
        wake_callback=wakes.append,
    )

    assert driver.cancel(session_id="session-1", reason="user") is cancel_result
    assert wakes == (["session-1"] if cancel_result else [])
