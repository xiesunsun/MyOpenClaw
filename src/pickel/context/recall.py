"""Recall：窄召回源协议。

RuntimeEffects 对 recall_sources 逐源 await provide，ModelContextBuilder
只接收已经取得的 recalled_messages。
OpenViking session recall 经 adapter 实现本协议。
"""

from __future__ import annotations

from typing import Protocol

from pickel.conversations.agent_message import AgentMessage


class Recall(Protocol):
    """可注入模型上下文的召回源。"""

    async def provide(
        self,
        *,
        session_id: str,
        current_user_text: str = "",
    ) -> list[AgentMessage]:
        """返回待追加的消息（优先 AgentMessage）。"""
        ...
