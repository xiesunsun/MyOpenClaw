"""会话产生、供 Surface 消费的附加输出合同。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Awaitable, Callable
from uuid import uuid4


@dataclass(frozen=True)
class ConversationOutputBase:
    output_id: str = field(default_factory=lambda: str(uuid4()))
    session_id: str = ""
    turn_id: str = ""
    source: str = ""
    occurred_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(frozen=True)
class AudioContent:
    data: bytes
    media_type: str
    sample_rate: int | None = None
    channels: int | None = None


@dataclass(frozen=True)
class AudioOutputReady(ConversationOutputBase):
    audio: AudioContent = field(
        default_factory=lambda: AudioContent(data=b"", media_type="audio/wav")
    )


@dataclass(frozen=True)
class AudioOutputFailed(ConversationOutputBase):
    message: str = ""
    retryable: bool = False


ConversationOutputHandler = Callable[
    [ConversationOutputBase],
    Awaitable[None] | None,
]
