"""AgentRegistry 的唯一 live 引用和幂等唤醒合同。"""

from __future__ import annotations

import asyncio

import pytest

from pickel.runtime.agent_registry import AgentRegistry


class _Agent:
    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.calls = 0
        self.active = 0
        self.max_active = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.second_call = asyncio.Event()

    async def when_idle(self) -> None:
        self.calls += 1
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.started.set()
        try:
            if self.calls == 1:
                await self.release.wait()
            else:
                self.second_call.set()
        finally:
            self.active -= 1


def test_register_rejects_two_live_agents_for_one_session() -> None:
    registry = AgentRegistry()
    first = _Agent("session-1")
    second = _Agent("session-1")

    registry.register(first)
    registry.register(first)
    assert registry.get("session-1") is first
    with pytest.raises(ValueError, match="live Agent"):
        registry.register(second)


def test_unregister_requires_current_agent_identity() -> None:
    registry = AgentRegistry()
    old = _Agent("session-1")
    new = _Agent("session-1")

    registry.register(old)
    assert registry.unregister("session-1", old)
    registry.register(new)

    assert not registry.unregister("session-1", old)
    assert registry.get("session-1") is new
    assert registry.unregister("session-1", new)


def test_wake_coalesces_running_task_and_preserves_followup_wake() -> None:
    async def scenario() -> None:
        registry = AgentRegistry()
        agent = _Agent("session-1")
        registry.register(agent)

        registry.wake("session-1")
        await agent.started.wait()
        registry.wake("session-1")
        registry.wake("session-1")
        agent.release.set()
        await agent.second_call.wait()

        assert agent.calls == 2
        assert agent.max_active == 1
        assert registry.unregister("session-1", agent)

    asyncio.run(scenario())


def test_shutdown_waits_for_running_wake_task() -> None:
    async def scenario() -> None:
        registry = AgentRegistry()
        agent = _Agent("session-1")
        registry.register(agent)
        registry.wake("session-1")
        await agent.started.wait()

        await registry.shutdown()

        assert registry.get("session-1") is None
        assert agent.active == 0

    asyncio.run(scenario())


def test_running_agent_can_unregister_itself_at_terminal_boundary() -> None:
    async def scenario() -> None:
        registry = AgentRegistry()
        completed = asyncio.Event()

        class SelfRemovingAgent:
            session_id = "session-1"

            async def when_idle(self) -> None:
                assert registry.unregister(self.session_id, self)
                completed.set()

        agent = SelfRemovingAgent()
        registry.register(agent)
        registry.wake(agent.session_id)
        await completed.wait()
        await asyncio.sleep(0)

        assert registry.get(agent.session_id) is None
        assert agent.session_id not in registry._tasks

    asyncio.run(scenario())


def test_headless_agent_retires_after_idle_check_and_runs_cleanup() -> None:
    async def scenario() -> None:
        registry = AgentRegistry()
        agent = _Agent("session-1")
        agent.release.set()
        retired = asyncio.Event()

        async def can_retire() -> bool:
            return True

        async def on_retire() -> None:
            retired.set()

        registry.register(
            agent,
            headless=True,
            can_retire=can_retire,
            on_retire=on_retire,
        )
        registry.wake(agent.session_id)
        await retired.wait()

        assert registry.get(agent.session_id) is None
        await registry.shutdown()

    asyncio.run(scenario())


def test_headless_retirement_race_preserves_followup_wake() -> None:
    async def scenario() -> None:
        registry = AgentRegistry()
        agent = _Agent("session-1")
        agent.release.set()
        checking = asyncio.Event()
        release = asyncio.Event()
        retired = asyncio.Event()
        checks = 0

        async def can_retire() -> bool:
            nonlocal checks
            checks += 1
            if checks == 1:
                checking.set()
                await release.wait()
            return True

        async def on_retire() -> None:
            retired.set()

        registry.register(
            agent,
            headless=True,
            can_retire=can_retire,
            on_retire=on_retire,
        )
        registry.wake(agent.session_id)
        await checking.wait()
        registry.wake(agent.session_id)
        release.set()
        await retired.wait()

        assert agent.calls == 2
        assert checks == 2
        assert registry.get(agent.session_id) is None
        await registry.shutdown()

    asyncio.run(scenario())


def test_retired_headless_agent_reactivates_on_external_wake() -> None:
    async def scenario() -> None:
        registry = AgentRegistry()
        first = _Agent("session-1")
        first.release.set()
        second = _Agent("session-1")
        second.release.set()
        retired = asyncio.Event()
        reactivated = asyncio.Event()

        async def can_retire() -> bool:
            return True

        async def on_retire() -> None:
            retired.set()

        async def wake_missing() -> None:
            registry.register(
                second,
                headless=True,
                can_retire=can_retire,
                on_retire=on_retire,
            )
            reactivated.set()

        registry.register(
            first,
            headless=True,
            can_retire=can_retire,
            on_retire=on_retire,
            wake_missing=wake_missing,
        )
        registry.wake(first.session_id)
        await retired.wait()
        registry.wake(first.session_id)
        await reactivated.wait()
        registry.wake(second.session_id)
        await asyncio.sleep(0)

        assert registry.get(first.session_id) is second
        await registry.shutdown()

    asyncio.run(scenario())
