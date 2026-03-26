from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, TYPE_CHECKING

from myopenclaw.conversation.message import Message

if TYPE_CHECKING:
    from myopenclaw.llm.config import ModelConfig


@dataclass
class ChatRequest:
    system_instruction: str | None
    messages: list[Message]


@dataclass
class ChatResult:
    text: str
    raw: Any | None = None


class BaseLLMProvider(ABC):
    @classmethod
    @abstractmethod
    def from_config(cls, config: "ModelConfig") -> "BaseLLMProvider":
        raise NotImplementedError

    @abstractmethod
    async def chat(self, request: ChatRequest) -> ChatResult:
        raise NotImplementedError
