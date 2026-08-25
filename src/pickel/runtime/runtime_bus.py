"""一个 ConversationRuntime 的 I/O 组合边界。"""

from __future__ import annotations

from pickel.runtime.event_bus import EventBus
from pickel.runtime.host_calls import HostCallRouter


class RuntimeBus:
    def __init__(
        self,
        *,
        events: EventBus | None = None,
    ) -> None:
        self.events = events or EventBus()
        self.host_calls = HostCallRouter()

    async def close(self) -> None:
        await self.host_calls.close()

    def close_now(self) -> None:
        self.host_calls.close_now()
