"""旧 ConversationContextService（迁移窗口，Task 8 前）。

**已废弃**：请改用 `ContextAssembler` 作为唯一 ModelContext 组装路径。
本类仍服务 Task 8 前的 ReAct / session_recall 旧 SessionMessage 路径，
不消费 Session entry 树，也不实现 compaction / tool 原子窗口。
"""

from __future__ import annotations

import warnings

from myopenclaw.context.models import UserTurn
from myopenclaw.conversations.message import MessageRole
from myopenclaw.conversations.message import SessionMessage
from myopenclaw.conversations.session import Session


class ConversationContextService:
    """Deprecated: 使用 ContextAssembler.assemble 替代。"""

    def __init__(self, *, cli_turn_window: int = 5) -> None:
        self.cli_turn_window = max(1, cli_turn_window)

    def collect_recent_user_turns(self, session: Session) -> list[UserTurn]:
        warnings.warn(
            "ConversationContextService 已废弃，请改用 ContextAssembler",
            DeprecationWarning,
            stacklevel=2,
        )
        # 旧线性 messages API 已移除；无 messages 时返回空，避免硬崩调用方
        linear_messages = getattr(session, "messages", None)
        if not linear_messages:
            return []

        recent_turns_reversed: list[UserTurn] = []
        assistant_messages_reversed: list[SessionMessage] = []

        for message in reversed(linear_messages):
            if message.role == MessageRole.ASSISTANT:
                assistant_messages_reversed.append(message)
                continue
            if message.role != MessageRole.USER:
                continue

            recent_turns_reversed.append(
                UserTurn(
                    user_message=message,
                    assistant_messages=list(reversed(assistant_messages_reversed)),
                )
            )
            assistant_messages_reversed = []
            if len(recent_turns_reversed) >= self.cli_turn_window:
                break

        recent_turns_reversed.reverse()
        return recent_turns_reversed

    def build_prompt_messages_from_session(
        self,
        session: Session,
        *,
        session_recall_message: SessionMessage | None = None,
    ) -> list[SessionMessage]:
        return self.build_prompt_messages_from_turns(
            self.collect_recent_user_turns(session),
            session_recall_message=session_recall_message,
        )

    @staticmethod
    def build_prompt_messages_from_turns(
        turns: list[UserTurn],
        *,
        session_recall_message: SessionMessage | None = None,
    ) -> list[SessionMessage]:
        messages: list[SessionMessage] = []
        if session_recall_message is not None:
            messages.append(session_recall_message)
        for turn in turns:
            messages.append(turn.user_message)
            messages.extend(turn.assistant_messages)
        return messages
