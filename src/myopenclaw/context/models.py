from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from myopenclaw.conversations.message import MessageRole, SessionMessage


@dataclass(frozen=True)
class ToolStep:
    message_index: int
    message: SessionMessage

    def __post_init__(self) -> None:
        if self.message.role != MessageRole.ASSISTANT or self.message.tool_call_batch is None:
            raise ValueError("ToolStep requires an assistant SessionMessage with tool_call_batch.")


@dataclass
class ConversationTurn:
    turn_index: int
    user_message_index: int
    user_message: SessionMessage
    tool_steps: list[ToolStep] = field(default_factory=list)
    final_answer_index: int | None = None
    final_answer: SessionMessage | None = None


@dataclass
class ConversationWindow:
    last_consumed_message_index: int = -1
    next_turn_index: int = 0
    completed_turns: deque[ConversationTurn] = field(default_factory=deque)
    current_turn: ConversationTurn | None = None


@dataclass
class ContextRuntimeStore:
    windows: dict[str, ConversationWindow] = field(default_factory=dict)


@dataclass(frozen=True)
class EffectiveContextSnapshot:
    session_id: str
    messages: list[SessionMessage]
    fingerprint: str
    completed_turn_count: int
    current_turn_tool_step_count: int
    trimmed_turn_count: int
