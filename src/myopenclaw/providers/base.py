from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from myopenclaw.context.model_context import ModelContext
from myopenclaw.conversations.agent_message import AssistantMessage

if TYPE_CHECKING:
    from myopenclaw.shared.model_config import ModelConfig


class BaseLLMProvider(ABC):
    @classmethod
    @abstractmethod
    def from_config(cls, config: "ModelConfig") -> "BaseLLMProvider":
        raise NotImplementedError

    @abstractmethod
    async def generate(self, context: ModelContext) -> AssistantMessage:
        """消费 ModelContext，返回统一 AssistantMessage。"""
        raise NotImplementedError

    async def count_context_tokens(self, context: ModelContext) -> int | None:
        """统计上下文 token；失败返回 None。"""
        return None
