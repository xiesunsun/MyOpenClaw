"""会话级 Runtime 事件处理器合同。"""

from __future__ import annotations

from collections.abc import Coroutine
from dataclasses import dataclass
from typing import Any, Callable, Protocol

from pickel.shared.conversation_output import ConversationOutputBase
from pickel.runtime.runtime_events import RuntimeEventBase
from pickel.shared.conversation_mode import ConversationMode

OutputPublisher = Callable[[ConversationOutputBase], Coroutine[Any, Any, None]]
BackgroundTaskStarter = Callable[[Coroutine[Any, Any, None], str], None]


@dataclass(frozen=True)
class ConversationExtensionContext:
    """会话 extension 可以使用的最小能力集合。"""

    agent_id: str
    session_id: str
    mode: ConversationMode
    publish_output: OutputPublisher
    start_background_task: BackgroundTaskStarter


class EventProcessor(Protocol):
    """把只读 Runtime 事件转换成可选的会话输出。"""

    async def handle_event(self, event: RuntimeEventBase) -> None: ...

    def close(self) -> None: ...


ConversationProcessorFactory = Callable[
    [ConversationExtensionContext],
    EventProcessor | None,
]


@dataclass(frozen=True)
class EventProcessorRegistration:
    extension_name: str
    event_types: tuple[type[Any], ...]
    factory: ConversationProcessorFactory


@dataclass(frozen=True)
class ResolvedEventProcessor:
    processor: EventProcessor
    event_types: tuple[type[Any], ...]
