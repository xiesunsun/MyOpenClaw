from dataclasses import dataclass
from typing import Any

from myopenclaw.conversation.metadata import MessageMetadata
from myopenclaw.conversation.message import Message
from myopenclaw.llm.metadata import TokenUsage


@dataclass
class ChatRequest:
    system_instruction: str | None
    messages: list[Message]


@dataclass
class ChatResult:
    text: str
    usage: TokenUsage | None = None
    metadata: MessageMetadata | None = None
    raw: Any | None = None
