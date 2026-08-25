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
        archived_at=archived_at,
    )


def _driver(session, operation_driver):
    return AgentDriver(
        conversation_store=_ConversationStore(session),
        inbox_store=object(),
        operation_service=object(),
        operation_driver=operation_driver,
        package_resolver=lambda **_: ("package-1", None),
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
