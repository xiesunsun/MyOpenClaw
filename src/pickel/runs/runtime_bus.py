"""一个 RuntimeConversation 的 I/O 组合边界。"""

from __future__ import annotations

from pickel.runs.event_bus import EventBus
from pickel.runs.host_calls import HostCallRecorder, HostCallRouter


class RuntimeBus:
    def __init__(
        self,
        *,
        events: EventBus | None = None,
        host_call_recorder: HostCallRecorder | None = None,
    ) -> None:
        self.events = events or EventBus()
        self.host_calls = HostCallRouter(host_call_recorder)

    async def close(self) -> None:
        await self.host_calls.close()

    def close_now(self) -> None:
        self.host_calls.close_now()
