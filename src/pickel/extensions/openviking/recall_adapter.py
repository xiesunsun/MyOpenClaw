"""OpenViking session recall → context.Recall 适配。"""

from __future__ import annotations

from pickel.context.session_recall import (
    SessionRecallProvider,
    render_session_recall,
)
from pickel.conversations.agent_message import AgentMessage, UserMessage
from pickel.conversations.content_blocks import TextContent


class OpenVikingRecall:
    """包装 SessionRecallProvider，输出 UserMessage 供上下文构建器注入。"""

    def __init__(
        self,
        provider: SessionRecallProvider,
        *,
        max_chars: int = 6000,
    ) -> None:
        self._provider = provider
        self._max_chars = max_chars

    async def provide(
        self,
        *,
        session_id: str,
        current_user_text: str = "",
    ) -> list[AgentMessage]:
        result = await self._provider.recall(
            session_id=session_id,
            current_user_text=current_user_text,
        )
        rendered = render_session_recall(result, max_chars=self._max_chars)
        if rendered is None:
            return []
        return [UserMessage(content=[TextContent(text=rendered)])]
