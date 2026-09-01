"""Agent 的持久化 Inbox 窄投影。

AgentInbox 不缓存第二份队列；每次读取都从 InboxStore 获取事实，因此重启后与
进程内运行期间拥有同一顺序和状态。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from uuid import uuid4

from pickel.conversations.agent_message import UserMessage
from pickel.inbox.message import (
    AgentMessageSource,
    InboxMessage,
    MessageDelivery,
    UserMessageSource,
)
from pickel.inbox.store import InboxStore


class AgentInbox:
    """一个 Agent 对所属 Session Inbox 的异步、幂等操作面。"""

    def __init__(
        self,
        *,
        session_id: str,
        store: InboxStore,
        sender_session_id: str | None = None,
        sender_operation_id: str | None = None,
        message_id_factory: Callable[[], str] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if not session_id:
            raise ValueError("session_id 不能为空")
        self._session_id = session_id
        self._store = store
        self._sender_session_id = sender_session_id
        self._sender_operation_id = sender_operation_id
        self._message_id_factory = message_id_factory or (lambda: str(uuid4()))
        self._now = now or (lambda: datetime.now(timezone.utc))

    @property
    def session_id(self) -> str:
        return self._session_id

    async def send(
        self,
        message: UserMessage,
        *,
        delivery: MessageDelivery = "followup",
    ) -> str:
        if self._sender_session_id is not None:
            if self._sender_operation_id is None:
                raise ValueError("sender_session_id 必须同时提供 sender_operation_id")
            source = AgentMessageSource(
                sender_session_id=self._sender_session_id,
                sender_operation_id=self._sender_operation_id,
                form=delivery,
            )
        else:
            source = UserMessageSource()
        stored = self._store.send_message(
            message_id=self._message_id_factory(),
            session_id=self._session_id,
            delivery=delivery,
            message=message,
            source=source,
            created_at=self._now(),
        )
        return stored.message_id

    async def list_pending(self) -> tuple[InboxMessage, ...]:
        return self._store.list_pending(session_id=self._session_id)

    async def discard(self, message_id: str, reason: str) -> bool:
        return self._store.discard_message(
            message_id=message_id,
            reason=reason,
            handled_at=self._now(),
        )

    async def clear(self, reason: str) -> int:
        pending = await self.list_pending()
        count = 0
        for message in pending:
            if await self.discard(message.message_id, reason):
                count += 1
        return count
