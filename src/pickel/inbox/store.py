"""Inbox 的数据库端口与内存投影端口。"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from pickel.conversations.agent_message import UserMessage
from pickel.inbox.message import (
    InboxMessage,
    MessageDelivery,
    MessageSource,
)


class InboxStore(Protocol):
    def send_message(
        self,
        *,
        message_id: str,
        session_id: str,
        delivery: MessageDelivery,
        message: UserMessage,
        source: MessageSource,
        created_at: datetime,
    ) -> InboxMessage: ...

    def list_pending(
        self, *, session_id: str, delivery: MessageDelivery | None = None
    ) -> tuple[InboxMessage, ...]: ...

    def claim_message(
        self,
        *,
        message_id: str,
        operation_id: str,
        step_id: str | None,
        handled_at: datetime,
    ) -> bool: ...

    def discard_message(
        self, *, message_id: str, reason: str, handled_at: datetime
    ) -> bool: ...
