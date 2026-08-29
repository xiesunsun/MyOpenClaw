"""HistoryCompaction 的生成接缝。

这里只定义 Runtime 组合所需的窄协议：触发方传入投影节点、已构建的
ModelContext 与预检结果，实现方返回待追加的 HistoryCompaction 值。
worker 调用、摘要模型、边界选取与提示词都属于可替换实现，不进入
Context 核心，也不反向依赖 Runtime。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Protocol

from pickel.context.model_context import ModelContext
from pickel.context.token_preflight import TokenPreflightResult
from pickel.conversations.conversation_node import ConversationNode, HistoryCompaction

if TYPE_CHECKING:
    from pickel.conversations.agent_message import AssistantMessage


class HistoryCompactionError(RuntimeError):
    """Generator 无法安全地产出 HistoryCompaction。"""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class SummarizerSender(Protocol):
    """绑定了一次 Operation 的可靠 worker 摘要调用。

    记账、退避重试与逐次落库由实现方负责；协议只要求给一份
    ModelContext，返回摘要模型的 AssistantMessage。
    """

    async def __call__(
        self, *, context: ModelContext, purpose: str
    ) -> AssistantMessage: ...


class HistoryCompactionGenerator(Protocol):
    """从一次明确的压缩请求生成 Conversation 内容值。"""

    async def generate(
        self,
        *,
        nodes: Sequence[ConversationNode],
        model_context: ModelContext,
        preflight: TokenPreflightResult,
        send_summarizer: SummarizerSender,
    ) -> HistoryCompaction:
        """返回待追加的值；不得删除或改写已有 ConversationNode。"""
        ...


__all__ = [
    "HistoryCompactionError",
    "HistoryCompactionGenerator",
    "SummarizerSender",
]
