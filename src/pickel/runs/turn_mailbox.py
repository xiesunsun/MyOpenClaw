"""活动 turn 的内存输入邮箱。

这里只提供排队与原子消费机制；不认识 Session、Hook、EventBus 或界面。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from typing import Literal, Protocol
from uuid import uuid4

from pickel.conversations.agent_message import UserMessage
from pickel.conversations.content_blocks import UserContent

InputDelivery = Literal["steer", "follow_up"]


@dataclass(frozen=True)
class PendingInput:
    """尚未交付给模型的用户输入。"""

    input_id: str
    content: tuple[UserContent, ...]
    delivery: InputDelivery
    target_turn_id: str
    revision: int = 1

    @property
    def message(self) -> UserMessage:
        return UserMessage(content=list(self.content))

    @classmethod
    def create(
        cls,
        *,
        message: UserMessage,
        delivery: InputDelivery,
        target_turn_id: str,
    ) -> "PendingInput":
        return cls(
            input_id=str(uuid4()),
            content=tuple(message.content),
            delivery=delivery,
            target_turn_id=target_turn_id,
        )


class TurnInputReader(Protocol):
    """ExecutionStrategy 可取得的只读输入端口。"""

    async def take_steering(self) -> PendingInput | None: ...

    async def finish_or_take_steering(self) -> PendingInput | None: ...

    async def put_back(self, item: PendingInput) -> None: ...


class TurnMailbox(TurnInputReader):
    """单个活动 turn 的 steering 邮箱。"""

    def __init__(self, turn_id: str) -> None:
        self.turn_id = turn_id
        self._items: list[PendingInput] = []
        self._closed = False
        self._lock = asyncio.Lock()

    async def add(self, message: UserMessage) -> PendingInput:
        async with self._lock:
            if self._closed:
                raise TurnMailboxClosedError("当前 turn 已进入结束阶段")
            item = PendingInput.create(
                message=message,
                delivery="steer",
                target_turn_id=self.turn_id,
            )
            self._items.append(item)
            return item

    async def update(
        self,
        input_id: str,
        message: UserMessage,
        *,
        expected_revision: int,
    ) -> PendingInput | None:
        async with self._lock:
            for index, item in enumerate(self._items):
                if item.input_id != input_id:
                    continue
                if item.revision != expected_revision:
                    raise PendingInputConflictError(
                        f"待执行输入版本不匹配：expected={expected_revision}, "
                        f"actual={item.revision}"
                    )
                updated = replace(
                    item,
                    content=tuple(message.content),
                    revision=item.revision + 1,
                )
                self._items[index] = updated
                return updated
            return None

    async def cancel(
        self,
        input_id: str,
        *,
        expected_revision: int,
    ) -> PendingInput | None:
        async with self._lock:
            for index, item in enumerate(self._items):
                if item.input_id != input_id:
                    continue
                if item.revision != expected_revision:
                    raise PendingInputConflictError(
                        f"待执行输入版本不匹配：expected={expected_revision}, "
                        f"actual={item.revision}"
                    )
                return self._items.pop(index)
            return None

    async def take_steering(self) -> PendingInput | None:
        async with self._lock:
            return self._items.pop(0) if self._items else None

    async def finish_or_take_steering(self) -> PendingInput | None:
        """原子取得最后一条 steering；为空时关闭输入，消除结束竞态。"""
        async with self._lock:
            if self._items:
                return self._items.pop(0)
            self._closed = True
            return None

    async def put_back(self, item: PendingInput) -> None:
        async with self._lock:
            if not self._closed:
                self._items.insert(0, item)

    async def snapshot(self) -> tuple[PendingInput, ...]:
        async with self._lock:
            return tuple(self._items)

    async def close_and_drain(self) -> tuple[PendingInput, ...]:
        async with self._lock:
            self._closed = True
            items = tuple(self._items)
            self._items.clear()
            return items


class TurnMailboxClosedError(RuntimeError):
    pass


class PendingInputConflictError(RuntimeError):
    pass
