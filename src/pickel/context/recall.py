"""Recall：窄召回源协议。

prepare 的 resolve_recalls 对 recall_sources 逐源调用 provide，
将返回的消息拼入 history 之后、hook feedback 之前。
默认无实现；P3 再挂 SessionRecall / OpenViking 等。
"""

from __future__ import annotations

from typing import Any, Protocol

from pickel.conversations.agent_message import AgentMessage


class Recall(Protocol):
    """可注入模型上下文的召回源。"""

    def provide(self, *, run: Any, session: Any) -> list[AgentMessage]:
        """返回待追加的消息（优先 AgentMessage）。"""
        ...
