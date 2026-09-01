"""进程内 Session 到 live Agent 的唯一映射和幂等唤醒。"""

from __future__ import annotations

import asyncio
import inspect
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Awaitable, Callable

if TYPE_CHECKING:
    from pickel.runtime.agent import Agent

logger = logging.getLogger(__name__)


async def _await_if_needed(value):
    if inspect.isawaitable(value):
        return await value
    return value


@dataclass
class _AgentEntry:
    agent: "Agent"
    drive: Callable[[], Awaitable[object]] | None
    headless: bool
    handle_count: int = 0
    can_retire: Callable[[], Awaitable[bool]] | None = None
    on_retire: Callable[[], Awaitable[None]] | None = None


class AgentHandle:
    """一个调用方对精确 live AgentEntry 的幂等引用。"""

    def __init__(self, registry: "AgentRegistry", session_id: str, entry: _AgentEntry):
        self.agent = entry.agent
        self._registry = registry
        self._session_id = session_id
        self._entry = entry
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._registry._release_handle(self._session_id, self._entry)


class AgentRegistry:
    """保存 live Agent、后台驱动和 headless Agent 的退休边界。"""

    def __init__(self) -> None:
        self._agents: dict[str, Agent] = {}
        self._entries: dict[str, _AgentEntry] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._wake_pending: set[str] = set()
        self._drives: dict[str, Callable[[], Awaitable[object]]] = {}
        self._shutting_down = False
        self._wake_missing: dict[str, Callable[[], Awaitable[None]]] = {}
        self._wake_missing_tasks: dict[str, asyncio.Task[None]] = {}

    def register(
        self,
        agent: "Agent",
        *,
        drive: Callable[[], Awaitable[object]] | None = None,
        headless: bool = False,
        can_retire: Callable[[], Awaitable[bool]] | None = None,
        on_retire: Callable[[], Awaitable[None]] | None = None,
        wake_missing: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        session_id = agent.session_id
        existing = self._agents.get(session_id)
        if existing is agent:
            return
        if existing is not None:
            raise ValueError(f"Session 已有 live Agent: {session_id}")
        if self._shutting_down:
            raise RuntimeError("AgentRegistry 已关闭")
        if headless and can_retire is None:
            raise ValueError("headless Agent 必须提供 can_retire")
        # 普通注册代表一个前台持有者；headless 注册没有前台 handle，
        # 只在一次 drive 确认无 runnable work 后退休。
        entry = _AgentEntry(
            agent=agent,
            drive=drive,
            headless=headless,
            handle_count=0 if headless else 1,
            can_retire=can_retire,
            on_retire=on_retire,
        )
        self._agents[session_id] = agent
        self._entries[session_id] = entry
        if drive is not None:
            self._drives[session_id] = drive
        if headless and wake_missing is not None:
            self._wake_missing[session_id] = wake_missing

    async def acquire(self, session_id: str) -> AgentHandle:
        if self._shutting_down:
            raise RuntimeError("AgentRegistry 已关闭")
        entry = self._entries.get(session_id)
        if entry is None:
            raise LookupError(f"Session 没有 live Agent: {session_id}")
        entry.handle_count += 1
        return AgentHandle(self, session_id, entry)

    def adopt(self, session_id: str, expected_agent: "Agent") -> None:
        """将 headless Agent 精确交给前台 Conversation 持有。"""
        entry = self._entries.get(session_id)
        if entry is None or entry.agent is not expected_agent:
            raise LookupError(f"Session 没有可接管的 live Agent: {session_id}")
        entry.headless = False
        entry.handle_count = max(1, entry.handle_count)
        entry.can_retire = None
        entry.on_retire = None

    def get(self, session_id: str) -> "Agent | None":
        return self._agents.get(session_id)

    def list_live(self) -> tuple["Agent", ...]:
        return tuple(self._agents.values())

    def wake(self, session_id: str) -> None:
        """唤醒 Session；运行中的任务只登记一次待重试唤醒。"""

        if self._shutting_down:
            return
        agent = self._agents.get(session_id)
        if agent is None:
            callback = self._wake_missing.get(session_id)
            if callback is not None:
                existing = self._wake_missing_tasks.get(session_id)
                if existing is not None and not existing.done():
                    return
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError as exc:
                    raise RuntimeError(
                        "AgentRegistry.wake 必须运行在 asyncio 事件循环中"
                    ) from exc
                task = loop.create_task(callback())
                self._wake_missing_tasks[session_id] = task
                task.add_done_callback(
                    lambda completed: self._missing_wake_finished(session_id, completed)
                )
            return
        task = self._tasks.get(session_id)
        if task is not None and not task.done():
            self._wake_pending.add(session_id)
            return
        if task is not None:
            self._tasks.pop(session_id, None)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError as exc:
            raise RuntimeError(
                "AgentRegistry.wake 必须运行在 asyncio 事件循环中"
            ) from exc
        task = loop.create_task(self._drive(session_id, agent))
        self._tasks[session_id] = task
        task.add_done_callback(
            lambda completed: self._task_finished(session_id, agent, completed)
        )

    def unregister(self, session_id: str, expected_agent: "Agent") -> bool:
        """仅移除仍由 expected_agent 占有的 live 引用。"""

        entry = self._entries.get(session_id)
        if entry is None or entry.agent is not expected_agent:
            return False
        self._remove_entry(session_id, entry)
        return True

    async def shutdown(self) -> None:
        """停止接受新激活，等待后台驱动后释放全部 AgentEntry。"""
        self._shutting_down = True
        tasks = tuple(self._tasks.values())
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        entries = tuple(self._entries.items())
        self._agents.clear()
        self._entries.clear()
        self._drives.clear()
        self._tasks.clear()
        self._wake_pending.clear()
        for _session_id, entry in entries:
            if entry.on_retire is not None:
                try:
                    await _await_if_needed(entry.on_retire())
                except Exception:
                    logger.exception("关闭 headless Agent 资源失败")
        missing_tasks = tuple(self._wake_missing_tasks.values())
        self._wake_missing.clear()
        self._wake_missing_tasks.clear()
        for task in missing_tasks:
            if not task.done():
                task.cancel()
        if missing_tasks:
            await asyncio.gather(*missing_tasks, return_exceptions=True)

    async def _drive(self, session_id: str, agent: "Agent") -> None:
        while True:
            # 给调用方一个机会在 activate/acquire 返回后接管 headless Agent；
            # 这也避免 asyncio.run 在激活调用返回前立即退休新对象。
            if (
                self._entries.get(session_id) is not None
                and self._entries[session_id].headless
            ):
                await asyncio.sleep(0)
            self._wake_pending.discard(session_id)
            entry = self._entries.get(session_id)
            if entry is None or entry.agent is not agent:
                return
            drive = entry.drive or agent.when_idle
            await drive()
            if self._agents.get(session_id) is not agent:
                return
            if session_id in self._wake_pending:
                continue
            if entry.headless and await self._retire_if_idle(session_id, entry):
                return
            # 仅在明确收到后续 wake 时再次 drive，不进行内存轮询。
            return

    async def _retire_if_idle(self, session_id: str, entry: _AgentEntry) -> bool:
        if entry.handle_count or entry.can_retire is None:
            return False
        if not await _await_if_needed(entry.can_retire()):
            return False
        # can_retire 可能等待 Store I/O；重新检查精确 Entry 和内存竞态。
        current = self._entries.get(session_id)
        if (
            current is not entry
            or entry.handle_count
            or session_id in self._wake_pending
            or self._tasks.get(session_id) is not asyncio.current_task()
        ):
            return False
        self._remove_entry(session_id, entry)
        if entry.on_retire is not None:
            try:
                await _await_if_needed(entry.on_retire())
            except Exception:
                logger.exception("退休 headless Agent 资源释放失败: %s", session_id)
        return True

    async def _release_handle(self, session_id: str, entry: _AgentEntry) -> None:
        if entry.handle_count:
            entry.handle_count -= 1
        if (
            entry.headless
            and entry.handle_count == 0
            and self._agents.get(session_id) is entry.agent
            and session_id not in self._tasks
        ):
            self.wake(session_id)

    def _remove_entry(self, session_id: str, entry: _AgentEntry) -> None:
        if self._entries.get(session_id) is not entry:
            return
        self._entries.pop(session_id, None)
        self._agents.pop(session_id, None)
        self._drives.pop(session_id, None)
        if not entry.headless:
            self._wake_missing.pop(session_id, None)
        self._wake_pending.discard(session_id)
        task = self._tasks.pop(session_id, None)
        if task is not None and not task.done() and task is not asyncio.current_task():
            task.cancel()

    def _task_finished(
        self,
        session_id: str,
        agent: "Agent",
        task: asyncio.Task[None],
    ) -> None:
        if self._tasks.get(session_id) is task:
            self._tasks.pop(session_id, None)
        try:
            task.result()
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("Agent wake 驱动失败: session_id=%s", session_id)
        if self._agents.get(session_id) is agent and session_id in self._wake_pending:
            self._wake_pending.discard(session_id)
            self.wake(session_id)

    def _missing_wake_finished(self, session_id: str, task: asyncio.Task[None]) -> None:
        if self._wake_missing_tasks.get(session_id) is task:
            self._wake_missing_tasks.pop(session_id, None)
        try:
            task.result()
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("headless Agent 重激活失败")


__all__ = ["AgentHandle", "AgentRegistry"]
