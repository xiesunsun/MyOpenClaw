"""Root 与 Child 共用的 Agent 消息接口。"""

from __future__ import annotations

import asyncio

from pickel.conversations.agent_message import UserMessage
from pickel.runtime.agent_driver import AgentDriveResult, AgentDriver
from pickel.runtime.operation_driver import OperationDriveResult
from pickel.runtime.agent_inbox import AgentInbox


class Agent:
    """一个 Session 的平等消息、取消和等待接口。

    Agent 不持有 Conversation 树、Operation 状态或 Provider；这些事实分别属于
    Store、OperationService 和 OperationDriver。
    """

    def __init__(
        self,
        *,
        session_id: str,
        inbox: AgentInbox,
        driver: AgentDriver,
    ) -> None:
        if inbox.session_id != session_id:
            raise ValueError("Agent 与 AgentInbox 必须绑定同一 session_id")
        self._session_id = session_id
        self._inbox = inbox
        self._driver = driver
        self._drive_lock = asyncio.Lock()

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def inbox(self) -> AgentInbox:
        return self._inbox

    async def followup(self, message: UserMessage) -> str:
        return await self._inbox.send(message, delivery="followup")

    async def steer(self, message: UserMessage) -> str:
        return await self._inbox.send(message, delivery="steer")

    async def inject(self, message: UserMessage) -> str:
        return await self._inbox.send(message, delivery="inject")

    def cancel(self, *, reason: str) -> bool:
        return self._driver.cancel(session_id=self._session_id, reason=reason)

    async def when_idle(
        self, *, consume_delta=None, host_calls=None
    ) -> AgentDriveResult:
        async with self._drive_lock:
            return await self._driver.when_idle(
                session_id=self._session_id,
                consume_delta=consume_delta,
                host_calls=host_calls,
            )

    async def resume_operation(
        self, operation_id: str, *, consume_delta=None, host_calls=None
    ) -> OperationDriveResult:
        """恢复当前 Session 的指定 Operation；不绕过 AgentDriver 校验。"""
        async with self._drive_lock:
            return await self._driver.resume_operation(
                session_id=self._session_id,
                operation_id=operation_id,
                consume_delta=consume_delta,
                host_calls=host_calls,
            )
