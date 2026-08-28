"""HistoryCompaction 的生成接缝。

这里只定义 Runtime 组合所需的窄协议。历史选择、摘要模型、Prompt、目标长度和
失败重试都属于后续可替换实现，不在 Context 核心中提供默认策略。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from pickel.context.model_context import ModelContext
from pickel.context.token_preflight import TokenPreflightResult
from pickel.conversations.conversation_node import ConversationNode, HistoryCompaction


class HistoryCompactionError(RuntimeError):
    """Generator 无法安全地产出 HistoryCompaction。"""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class HistoryCompactionGenerator(Protocol):
    """从一次明确的压缩请求生成 Conversation 内容值。"""

    async def generate(
        self,
        *,
        session_id: str,
        nodes: Sequence[ConversationNode],
        model_context: ModelContext,
        preflight: TokenPreflightResult,
    ) -> HistoryCompaction:
        """返回待追加的值；不得删除或改写已有 ConversationNode。"""
        ...
