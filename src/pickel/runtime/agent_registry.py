"""进程内 Session 到 live Agent 的唯一映射和幂等唤醒。"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pickel.runtime.agent import Agent

logger = logging.getLogger(__name__)


class AgentRegistry:
    """只保存 live Agent 引用和每个 Session 的驱动任务。"""

    def __init__(self) -> None:
        self._agents: dict[str, Agent] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._wake_pending: set[str] = set()

    def register(self, agent: Agent) -> None:
        session_id = agent.session_id
        existing = self._agents.get(session_id)
        if existing is agent:
            return
        if existing is not None:
            raise ValueError(f"Session 已有 live Agent: {session_id}")
        self._agents[session_id] = agent

    def get(self, session_id: str) -> Agent | None:
        return self._agents.get(session_id)

    def wake(self, session_id: str) -> None:
        """唤醒 Session；运行中的任务只登记一次待重试唤醒。"""

        agent = self._agents.get(session_id)
        if agent is None:
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

    def unregister(self, session_id: str, expected_agent: Agent) -> bool:
        """仅移除仍由 expected_agent 占有的 live 引用。"""

        if self._agents.get(session_id) is not expected_agent:
            return False
        self._agents.pop(session_id, None)
        self._wake_pending.discard(session_id)
        task = self._tasks.pop(session_id, None)
        if task is not None and not task.done():
            task.cancel()
        return True

    async def shutdown(self) -> None:
        """停止并等待全部后台 wake task，避免资源先于驱动退出。"""
        tasks = tuple(self._tasks.values())
        self._agents.clear()
        self._tasks.clear()
        self._wake_pending.clear()
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _drive(self, session_id: str, agent: Agent) -> None:
        while True:
            self._wake_pending.discard(session_id)
            await agent.when_idle()
            if self._agents.get(session_id) is not agent:
                return
            if session_id not in self._wake_pending:
                return

    def _task_finished(
        self,
        session_id: str,
        agent: Agent,
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
