from __future__ import annotations

from myopenclaw.context.models import ConversationTurn, ConversationWindow, ToolStep
from myopenclaw.conversations.message import MessageRole, SessionMessage
from myopenclaw.conversations.session import Session


class ConversationWindowManager:
    def __init__(self, cli_turn_window: int = 5) -> None:
        self.cli_turn_window = max(1, cli_turn_window)

    def sync_with_session(
        self,
        *,
        session: Session,
        window: ConversationWindow,
    ) -> None:
        start_index = max(window.last_consumed_message_index + 1, 0)
        for message_index in range(start_index, len(session.messages)):
            self._consume_message(
                window=window,
                message_index=message_index,
                message=session.messages[message_index],
            )
            window.last_consumed_message_index = message_index

    def _consume_message(
        self,
        *,
        window: ConversationWindow,
        message_index: int,
        message: SessionMessage,
    ) -> None:
        if message.role == MessageRole.USER:
            self._start_turn(
                window=window,
                message_index=message_index,
                message=message,
            )
            return

        if message.role != MessageRole.ASSISTANT or window.current_turn is None:
            return

        if message.tool_call_batch is not None:
            self._append_tool_step(
                window=window,
                message_index=message_index,
                message=message,
            )
            return

        self._finish_turn(
            window=window,
            message_index=message_index,
            message=message,
        )

    def _start_turn(
        self,
        *,
        window: ConversationWindow,
        message_index: int,
        message: SessionMessage,
    ) -> None:
        if window.current_turn is not None:
            window.completed_turns.append(window.current_turn)
            self._trim_completed_turns(window)

        window.current_turn = ConversationTurn(
            turn_index=window.next_turn_index,
            user_message_index=message_index,
            user_message=message,
        )
        window.next_turn_index += 1

    def _append_tool_step(
        self,
        *,
        window: ConversationWindow,
        message_index: int,
        message: SessionMessage,
    ) -> None:
        window.current_turn.tool_steps.append(
            ToolStep(message_index=message_index, message=message)
        )

    def _finish_turn(
        self,
        *,
        window: ConversationWindow,
        message_index: int,
        message: SessionMessage,
    ) -> None:
        window.current_turn.final_answer_index = message_index
        window.current_turn.final_answer = message
        window.completed_turns.append(window.current_turn)
        window.current_turn = None
        self._trim_completed_turns(window)

    def _trim_completed_turns(self, window: ConversationWindow) -> None:
        while len(window.completed_turns) > self.cli_turn_window:
            window.completed_turns.popleft()
