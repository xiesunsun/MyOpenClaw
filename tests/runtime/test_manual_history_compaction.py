"""Agent 严格 idle 手动历史压缩入口。"""

import asyncio

from pickel.runtime.agent import Agent, ManualHistoryCompactionResult


class _Inbox:
    session_id = "session-1"


class _Driver:
    def __init__(self, started: asyncio.Event, release: asyncio.Event) -> None:
        self.started = started
        self.release = release

    async def when_idle(self, **_kwargs):
        self.started.set()
        await self.release.wait()


def test_manual_compaction_returns_busy_without_waiting_for_drive_lock() -> None:
    async def run():
        started = asyncio.Event()
        release = asyncio.Event()
        agent = Agent(
            session_id="session-1",
            inbox=_Inbox(),
            driver=_Driver(started, release),
            manual_history_idle_check=lambda: True,
            manual_history_compactor=lambda: _completed(),
        )
        driving = asyncio.create_task(agent.when_idle())
        await started.wait()
        result = await asyncio.wait_for(agent.compact_history(), timeout=0.05)
        release.set()
        await driving
        return result

    result = asyncio.run(run())
    assert result.code == "session_busy"


def test_manual_compaction_rechecks_idle_after_lock() -> None:
    async def run():
        checks = iter((True, False))
        called = False

        async def compact():
            nonlocal called
            called = True
            return await _completed()

        agent = Agent(
            session_id="session-1",
            inbox=_Inbox(),
            driver=_Driver(asyncio.Event(), asyncio.Event()),
            manual_history_idle_check=lambda: next(checks),
            manual_history_compactor=compact,
        )
        result = await agent.compact_history()
        return result, called

    result, called = asyncio.run(run())
    assert result.code == "session_busy"
    assert not called


async def _completed() -> ManualHistoryCompactionResult:
    return ManualHistoryCompactionResult(code="ok", message="历史压缩完成")
