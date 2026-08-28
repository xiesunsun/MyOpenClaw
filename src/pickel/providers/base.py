from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, AsyncIterator

from pickel.context.model_context import ModelContext
from pickel.providers.prepared import PreparedModelCall
from pickel.providers.stream import StreamDelta

if TYPE_CHECKING:
    from pickel.shared.model_config import ModelConfig


class Provider(ABC):
    # Provider 实际缓存匹配的语义顺序；仅用于诊断。
    request_cache_order: tuple[str, ...] = ()

    @classmethod
    @abstractmethod
    def from_config(cls, config: "ModelConfig") -> "Provider":
        raise NotImplementedError

    def prepare(self, context: ModelContext) -> PreparedModelCall:
        """把唯一 ModelContext 纯映射为即将发送的完整 wire body。"""
        raise NotImplementedError(
            f"{type(self).__name__} 未实现 PreparedModelCall mapper"
        )

    async def stream_prepared(
        self, prepared: PreparedModelCall
    ) -> AsyncIterator[StreamDelta]:
        """只消费已经持久化并通过发送闸门的 PreparedModelCall。"""
        del prepared
        raise NotImplementedError(
            f"{type(self).__name__} 未实现 PreparedModelCall 发送入口"
        )
        yield  # pragma: no cover

    async def count_context_tokens(self, context: ModelContext) -> int | None:
        """统计上下文 token；不是生成调用，不创建 ModelCall。"""
        return None
