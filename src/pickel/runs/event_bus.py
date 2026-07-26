"""EventBus：runtime 事件的多订阅广播。

两条硬约束：
1. seq 只在这里分配（红线 4）——它是全序的唯一来源
2. 订阅者异常被吞掉（红线 2）——渲染器崩溃不得杀掉正在跑的 turn
"""

from __future__ import annotations

import inspect
import logging
from dataclasses import replace
from typing import Callable

from pickel.runs.runtime_events import RuntimeEventBase, RuntimeEventHandler

logger = logging.getLogger(__name__)


class EventBus:
    def __init__(self) -> None:
        # 按订阅时分配的 id 为 key，而非按 handler 本身 == 匹配：
        # 绑定方法（如 renderer.handle_event）每次取值都是新对象，但 == 恒成立，
        # 若按值匹配退订会删错槽位、打乱剩余订阅者的调用顺序。
        self._handlers: dict[int, RuntimeEventHandler] = {}
        self._next_handler_id = 0
        self._next_seq = 0

    def subscribe(self, handler: RuntimeEventHandler) -> Callable[[], None]:
        """注册订阅者，返回退订函数。"""
        handler_id = self._next_handler_id
        self._next_handler_id += 1
        self._handlers[handler_id] = handler

        def unsubscribe() -> None:
            self._handlers.pop(handler_id, None)

        return unsubscribe

    async def emit(self, event: RuntimeEventBase) -> RuntimeEventBase:
        """分配 seq 后广播；返回带 seq 的事件。"""
        stamped = replace(event, envelope=event.envelope.with_seq(self._next_seq))
        self._next_seq += 1

        for handler in list(self._handlers.values()):
            try:
                result = handler(stamped)
                if inspect.isawaitable(result):
                    await result
            except Exception:  # noqa: BLE001 — 订阅者异常不得影响 turn
                identifier = getattr(handler, "__qualname__", repr(handler))
                logger.exception("事件订阅者异常，已隔离: %s", identifier)
        return stamped
