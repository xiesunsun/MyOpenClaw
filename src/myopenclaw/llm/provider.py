from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from myopenclaw.llm.chat_types import ChatRequest, ChatResult, MessageMetadata, TokenUsage

if TYPE_CHECKING:
    from myopenclaw.llm.config import ModelConfig


class BaseLLMProvider(ABC):
    @classmethod
    @abstractmethod
    def from_config(cls, config: "ModelConfig") -> "BaseLLMProvider":
        raise NotImplementedError

    @abstractmethod
    async def chat(self, request: ChatRequest) -> ChatResult:
        raise NotImplementedError
