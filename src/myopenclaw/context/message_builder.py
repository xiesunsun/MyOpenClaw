from __future__ import annotations

from myopenclaw.context.formatter import ToolStepFormatter
from myopenclaw.context.models import ConversationTurn, ConversationWindow, ToolStep
from myopenclaw.conversations.message import MessageRole, SessionMessage


class ConversationContextBuilder:
    def __init__(
        self,
        *,
        tool_arguments_preview_chars: int = 100,
        tool_result_preview_chars: int = 100,
    ) -> None:
        self.tool_step_formatter = ToolStepFormatter(
            tool_arguments_preview_chars=tool_arguments_preview_chars,
            tool_result_preview_chars=tool_result_preview_chars,
        )

    def build_messages(
        self,
        *,
        window: ConversationWindow,
    ) -> list[SessionMessage]:
        messages: list[SessionMessage] = []
        for turn in window.completed_turns:
            messages.extend(self._build_completed_turn_messages(turn))
        if window.current_turn is not None:
            messages.extend(self._build_current_turn_messages(window.current_turn))
        return messages

    def _build_completed_turn_messages(
        self,
        turn: ConversationTurn,
    ) -> list[SessionMessage]:
        messages = [turn.user_message]
        messages.extend(self._build_completed_tool_step_messages(turn.tool_steps))
        if turn.final_answer is not None:
            messages.append(turn.final_answer)
        return messages

    def _build_completed_tool_step_messages(
        self,
        tool_steps: list[ToolStep],
    ) -> list[SessionMessage]:
        messages: list[SessionMessage] = []
        for tool_step in tool_steps:
            summary = self.tool_step_formatter.format_summary(tool_step)
            if not summary:
                continue
            messages.append(
                SessionMessage(
                    role=MessageRole.ASSISTANT,
                    content=summary,
                )
            )
        return messages

    @staticmethod
    def _build_current_turn_messages(turn: ConversationTurn) -> list[SessionMessage]:
        messages = [turn.user_message]
        messages.extend(tool_step.message for tool_step in turn.tool_steps)
        if turn.final_answer is not None:
            messages.append(turn.final_answer)
        return messages
