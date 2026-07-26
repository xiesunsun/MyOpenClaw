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
        self._handlers: list[RuntimeEventHandler] = []
        self._next_seq = 0

    def subscribe(self, handler: RuntimeEventHandler) -> Callable[[], None]:
        """注册订阅者，返回退订函数。"""
        self._handlers.append(handler)

        def unsubscribe() -> None:
            if handler in self._handlers:
                self._handlers.remove(handler)

        return unsubscribe

    async def emit(self, event: RuntimeEventBase) -> RuntimeEventBase:
        """分配 seq 后广播；返回带 seq 的事件。"""
        stamped = replace(event, envelope=event.envelope.with_seq(self._next_seq))
        self._next_seq += 1

        for handler in list(self._handlers):
            try:
                result = handler(stamped)
                if inspect.isawaitable(result):
                    await result
            except Exception:  # noqa: BLE001 — 订阅者异常不得影响 turn
                logger.exception("事件订阅者异常，已隔离: %s", type(handler).__name__)
        return stamped
