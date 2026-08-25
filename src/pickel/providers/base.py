from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, AsyncIterator

from pickel.context.model_context import ModelContext
from pickel.conversations.agent_message import AssistantMessage
from pickel.providers.stream import StreamCompleted, StreamDelta

if TYPE_CHECKING:
    from pickel.shared.model_config import ModelConfig


class Provider(ABC):
    # Provider 实际缓存匹配的语义顺序；仅用于 full trace 诊断。
    request_cache_order: tuple[str, ...] = ()

    @classmethod
    @abstractmethod
    def from_config(cls, config: "ModelConfig") -> "Provider":
        raise NotImplementedError

    @abstractmethod
    async def generate(self, context: ModelContext) -> AssistantMessage:
        """消费 ModelContext，返回统一 AssistantMessage。"""
        raise NotImplementedError

    async def stream(self, context: ModelContext) -> AsyncIterator[StreamDelta]:
        """产出增量；默认实现不流式，一次性给出完整结果。

        覆写此方法即可获得真流式。覆写者必须让自己的 generate()
        由自己的 stream() 实现（`accumulate(self.stream(ctx))`），
        否则同一个 provider 会有两份解析逻辑，迟早漂移。
        """
        yield StreamCompleted(message=await self.generate(context))

    async def count_context_tokens(self, context: ModelContext) -> int | None:
        """统计上下文 token；失败返回 None。"""
        return None

    def request_snapshot(self, context: ModelContext) -> dict[str, Any] | None:
        """返回实际 Provider wire request；不支持时返回 None。"""
        return None
