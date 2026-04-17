from __future__ import annotations

from myopenclaw.context.models import UserTurn
from myopenclaw.conversations.message import MessageRole
from myopenclaw.conversations.message import SessionMessage
from myopenclaw.conversations.session import Session


class ConversationContextService:
    def __init__(self, *, cli_turn_window: int = 5) -> None:
        self.cli_turn_window = max(1, cli_turn_window)

    def collect_recent_user_turns(self, session: Session) -> list[UserTurn]:
        recent_turns_reversed: list[UserTurn] = []
        assistant_messages_reversed: list[SessionMessage] = []

        for message in reversed(session.messages):
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
