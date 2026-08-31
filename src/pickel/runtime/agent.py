"""Root 与 Child 共用的 Agent 消息接口。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from pickel.conversations.agent_message import UserMessage
from pickel.runtime.agent_driver import AgentDriveResult, AgentDriver
from pickel.runtime.operation_driver import OperationDriveResult
from pickel.runtime.agent_inbox import AgentInbox


class AgentBusyError(RuntimeError):
    """Agent 已有前台驱动占用时，拒绝新的原子 followup。"""


@dataclass(frozen=True)
class ManualHistoryCompactionResult:
    """手动压缩的稳定结果；该入口不创建 Operation。"""

    code: str
    message: str
    node_id: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.code == "ok"


ManualHistoryCompactor = Callable[[], Awaitable[ManualHistoryCompactionResult]]
ManualHistoryIdleCheck = Callable[[], bool]


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
        manual_history_compactor: ManualHistoryCompactor | None = None,
        manual_history_idle_check: ManualHistoryIdleCheck | None = None,
    ) -> None:
        if inbox.session_id != session_id:
            raise ValueError("Agent 与 AgentInbox 必须绑定同一 session_id")
        self._session_id = session_id
        self._inbox = inbox
        self._driver = driver
        self._drive_lock = asyncio.Lock()
        self._manual_history_compactor = manual_history_compactor
        self._manual_history_idle_check = manual_history_idle_check

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def inbox(self) -> AgentInbox:
        return self._inbox

    async def followup(self, message: UserMessage) -> str:
        message_id = await self._inbox.send(message, delivery="followup")
        self._driver.wake(self._session_id)
        return message_id

    async def followup_and_wait(
        self,
        message: UserMessage,
        *,
        consume_delta=None,
        consume_tool_event=None,
        consume_operation_accepted=None,
        host_calls=None,
    ) -> AgentDriveResult:
        """原子写入前台 followup 并立即驱动一次。

        该入口用于前台调用方把消息接受和驱动绑定为一个不可交错的操作：
        驱动已被占用时在写入 Inbox 前直接报告忙碌，不额外触发 wake。
        """
        if self._drive_lock.locked():
            raise AgentBusyError("Agent 当前正在驱动，不能接受前台 followup")
        async with self._drive_lock:
            await self._inbox.send(message, delivery="followup")
            kwargs = {
                "session_id": self._session_id,
                "consume_delta": consume_delta,
                "consume_tool_event": consume_tool_event,
                "host_calls": host_calls,
            }
            if consume_operation_accepted is not None:
                kwargs["consume_operation_accepted"] = consume_operation_accepted
            return await self._driver.when_idle(
                **kwargs,
            )

    async def steer(self, message: UserMessage) -> str:
        message_id = await self._inbox.send(message, delivery="steer")
        self._driver.wake(self._session_id)
        return message_id

    async def inject(self, message: UserMessage) -> str:
        return await self._inbox.send(message, delivery="inject")

    def cancel(self, *, reason: str) -> bool:
        return self._driver.cancel(session_id=self._session_id, reason=reason)

    async def when_idle(
        self, *, consume_delta=None, consume_tool_event=None, host_calls=None
    ) -> AgentDriveResult:
        async with self._drive_lock:
            return await self._driver.when_idle(
                session_id=self._session_id,
                consume_delta=consume_delta,
                consume_tool_event=consume_tool_event,
                host_calls=host_calls,
            )

    async def compact_history(self) -> ManualHistoryCompactionResult:
        """严格 idle 时手动压缩；忙碌立即返回，不等待 Agent 驱动。"""

        busy = ManualHistoryCompactionResult(
            code="session_busy", message="Session 当前繁忙，不能手动压缩历史"
        )
        if self._drive_lock.locked():
            return busy
        if self._manual_history_compactor is None:
            return ManualHistoryCompactionResult(
                code="history_compaction_unavailable",
                message="当前 Runtime 未配置手动历史压缩",
            )
        if (
            self._manual_history_idle_check is not None
            and not self._manual_history_idle_check()
        ):
            return busy
        async with self._drive_lock:
            # 获取同一个 drive lock 后由 Host 回读 Session 和 Inbox；这次检查
            # 是手动压缩真正开始前的最后一道 idle 门禁。
            if (
                self._manual_history_idle_check is not None
                and not self._manual_history_idle_check()
            ):
                return busy
            return await self._manual_history_compactor()

    def configure_manual_history_compaction(
        self,
        *,
        compactor: ManualHistoryCompactor,
        idle_check: ManualHistoryIdleCheck,
    ) -> None:
        """注入 Host 组合的手动压缩依赖。"""

        self._manual_history_compactor = compactor
        self._manual_history_idle_check = idle_check

    async def resume_operation(
        self,
        operation_id: str,
        *,
        consume_delta=None,
        consume_tool_event=None,
        host_calls=None,
    ) -> OperationDriveResult:
        """恢复当前 Session 的指定 Operation；不绕过 AgentDriver 校验。"""
        async with self._drive_lock:
            return await self._driver.resume_operation(
                session_id=self._session_id,
                operation_id=operation_id,
                consume_delta=consume_delta,
                consume_tool_event=consume_tool_event,
                host_calls=host_calls,
            )
