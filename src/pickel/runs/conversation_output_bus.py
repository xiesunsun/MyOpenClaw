"""会话附加输出的多订阅广播。"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Callable

from pickel.shared.conversation_output import (
    ConversationOutputBase,
    ConversationOutputHandler,
)

logger = logging.getLogger(__name__)


class ConversationOutputBus:
    """把 extension 产生的输出交给 CLI、Web 或 API 等 Surface。"""

    def __init__(self) -> None:
        self._handlers: dict[int, ConversationOutputHandler] = {}
        self._next_handler_id = 0

    def subscribe(
        self,
        handler: ConversationOutputHandler,
    ) -> Callable[[], None]:
        handler_id = self._next_handler_id
        self._next_handler_id += 1
        self._handlers[handler_id] = handler

        def unsubscribe() -> None:
            self._handlers.pop(handler_id, None)

        return unsubscribe

    async def publish(self, output: ConversationOutputBase) -> None:
        for handler in list(self._handlers.values()):
            try:
                result = handler(output)
                if inspect.isawaitable(result):
                    await result
            except Exception:  # noqa: BLE001 — Surface 失败不得影响输出生产者
                identifier = getattr(handler, "__qualname__", repr(handler))
                logger.exception("会话输出订阅者异常，已隔离: %s", identifier)

    def clear(self) -> None:
        self._handlers.clear()
